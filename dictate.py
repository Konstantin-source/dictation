import os
import sys
import json
import queue
import threading
import time
import tkinter as tk
from tkinter import messagebox
import numpy as np
import sounddevice as sd
import pyperclip

# Configuration defaults
DEFAULT_CONFIG = {
    "model_size": "small",
    "language": "de",
    "hotkey": "Ctrl+Alt+J",
    "auto_exit_delay_ms": 1500,
    "theme": "dark",
    "vad_filter": True,
    "initial_prompt": "Diktat auf Deutsch, enthält Software-Entwicklungs-Begriffe wie: git, commit, branch, push, pull request, merge, frontend, backend, refactoring, API, bug, code, deploy, database, pipeline, function, class, variable, loop, array, json, python."
}

CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")

def load_config():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                config = json.load(f)
                # Fill missing keys with defaults
                for k, v in DEFAULT_CONFIG.items():
                    if k not in config:
                        config[k] = v
                return config
        except Exception as e:
            print(f"Error loading config.json, using defaults: {e}")
    return DEFAULT_CONFIG.copy()

def install_shortcut(config):
    """Creates a Windows Shortcut (.lnk) on the desktop pointing to pythonw.exe running this script."""
    import subprocess
    
    script_path = os.path.abspath(__file__)
    script_dir = os.path.dirname(script_path)
    
    # Locate pythonw.exe (windowed Python, hides console window)
    pythonw_exe = sys.executable.replace("python.exe", "pythonw.exe")
    if not os.path.exists(pythonw_exe):
        # Fallback to standard python if pythonw is not found
        pythonw_exe = sys.executable

    desktop = os.path.expanduser("~/Desktop")
    shortcut_path = os.path.join(desktop, "GedankenDiktat.lnk")
    hotkey = config.get("hotkey", "Ctrl+Alt+J")

    # PowerShell script to create the Windows shortcut on Desktop AND Start Menu
    ps_command = f"""
    $WshShell = New-Object -ComObject WScript.Shell
    
    # Desktop Shortcut
    $Shortcut = $WshShell.CreateShortcut('{shortcut_path}')
    $Shortcut.TargetPath = '{pythonw_exe}'
    $Shortcut.Arguments = '"{script_path}"'
    $Shortcut.WorkingDirectory = '{script_dir}'
    $Shortcut.Description = 'GedankenDiktat: Gedanken schnell diktieren und kopieren'
    $Shortcut.Hotkey = '{hotkey}'
    $Shortcut.Save()
    
    # Start Menu Shortcut
    $StartMenuDir = [System.IO.Path]::Combine($env:APPDATA, 'Microsoft\\Windows\\Start Menu\\Programs')
    if (-not (Test-Path $StartMenuDir)) {{
        New-Item -ItemType Directory -Force -Path $StartMenuDir | Out-Null
    }}
    $StartMenuShortcut = [System.IO.Path]::Combine($StartMenuDir, 'GedankenDiktat.lnk')
    $Shortcut2 = $WshShell.CreateShortcut($StartMenuShortcut)
    $Shortcut2.TargetPath = '{pythonw_exe}'
    $Shortcut2.Arguments = '"{script_path}"'
    $Shortcut2.WorkingDirectory = '{script_dir}'
    $Shortcut2.Description = 'GedankenDiktat: Gedanken schnell diktieren und kopieren'
    $Shortcut2.Hotkey = '{hotkey}'
    $Shortcut2.Save()
    """
    
    try:
        subprocess.run(["powershell", "-Command", ps_command], check=True, capture_output=True)
        msg = f"Erfolgreich installiert!\n\nVerknüpfung wurde auf dem Desktop erstellt: GedankenDiktat.lnk\nHotkey: {hotkey}\n\nDu kannst das Programm jetzt jederzeit mit {hotkey} starten!"
        print(msg)
    except subprocess.CalledProcessError as e:
        err_msg = f"Fehler beim Erstellen der Verknüpfung:\n{e.stderr.decode('utf-8', errors='ignore')}"
        print(err_msg)
        sys.exit(1)


class DictationApp:
    def __init__(self, root, config):
        self.root = root
        self.config = config
        
        # Setup GUI Window Properties
        self.root.title("GedankenDiktat")
        self.root.configure(bg="#121214")
        
        # Center borderless window
        window_width = 460
        window_height = 240
        self.root.overrideredirect(True) # Make it borderless
        self.root.attributes("-topmost", True) # Keep on top
        
        screen_width = self.root.winfo_screenwidth()
        screen_height = self.root.winfo_screenheight()
        x = (screen_width - window_width) // 2
        y = (screen_height - window_height) // 2
        self.root.geometry(f"{window_width}x{window_height}+{x}+{y}")
        
        # Bind Drag events to allow moving the borderless window
        self.root.bind("<Button-1>", self.start_drag)
        self.root.bind("<B1-Motion>", self.drag)
        
        # Audio recording variables
        self.sample_rate = 16000 # Whisper expects 16kHz
        self.audio_queue = queue.Queue()
        self.recording = True
        self.recorded_audio = None
        self.recording_duration = 0.0
        
        # Background Whisper model loading
        self.model = None
        self.model_loaded = threading.Event()
        self.model_load_error = None
        
        # Start background model loading
        self.model_thread = threading.Thread(target=self.load_whisper_model, daemon=True)
        self.model_thread.start()
        
        # Setup UI elements
        self.setup_ui()
        
        # Start recording immediately
        self.start_audio_recording()
        
        # Bind hotkeys to stop/cancel
        self.root.bind("<space>", lambda e: self.stop_recording())
        self.root.bind("<Return>", lambda e: self.stop_recording())
        self.root.bind("<Escape>", lambda e: self.cancel())
        
        # Start timers and animations
        self.update_timer()
        self.pulse_radius = 16
        self.pulse_direction = 1
        self.animate_recording()

    def start_drag(self, event):
        self.drag_start_x = event.x
        self.drag_start_y = event.y

    def drag(self, event):
        x = self.root.winfo_x() + (event.x - self.drag_start_x)
        y = self.root.winfo_y() + (event.y - self.drag_start_y)
        self.root.geometry(f"+{x}+{y}")

    def setup_ui(self):
        # Outer border frame
        self.border_frame = tk.Frame(self.root, bg="#2E2E35", bd=2)
        self.border_frame.place(relx=0, rely=0, relwidth=1, relheight=1)
        
        # Inner content frame
        self.content_frame = tk.Frame(self.border_frame, bg="#121214")
        self.content_frame.place(relx=0.01, rely=0.01, relwidth=0.98, relheight=0.98)
        
        # Close button in the top right
        self.close_btn = tk.Label(self.content_frame, text="✕", fg="#6E6E73", bg="#121214", font=("Segoe UI", 11, "bold"), cursor="hand2")
        self.close_btn.place(x=420, y=10)
        self.close_btn.bind("<Button-1>", lambda e: self.cancel())
        self.close_btn.bind("<Enter>", lambda e: self.close_btn.configure(fg="#FF453A"))
        self.close_btn.bind("<Leave>", lambda e: self.close_btn.configure(fg="#6E6E73"))
        
        # Canvas for animated indicator (pulse / spinner)
        self.canvas = tk.Canvas(self.content_frame, width=80, height=80, bg="#121214", bd=0, highlightthickness=0)
        self.canvas.pack(pady=(25, 5))
        
        # Status Label
        self.status_label = tk.Label(self.content_frame, text="Gedanken aufnehmen...", fg="#FFFFFF", bg="#121214", font=("Segoe UI", 15, "bold"))
        self.status_label.pack(pady=5)
        
        # Timer / Info Label
        self.info_label = tk.Label(self.content_frame, text="00:00", fg="#8E8E93", bg="#121214", font=("Segoe UI", 11))
        self.info_label.pack()
        
        # Shortcuts / Help footer
        self.footer_label = tk.Label(
            self.content_frame, 
            text="[Leertaste / Enter] Fertig  |  [Esc] Abbrechen", 
            fg="#545458", 
            bg="#121214", 
            font=("Segoe UI", 9)
        )
        self.footer_label.pack(side="bottom", pady=(0, 15))

    def start_audio_recording(self):
        try:
            self.stream = sd.InputStream(
                samplerate=self.sample_rate,
                channels=1,
                dtype='float32',
                callback=self.audio_callback
            )
            self.stream.start()
            self.start_time = time.time()
        except Exception as e:
            self.show_error(f"Mikrofon-Fehler:\n{e}")

    def audio_callback(self, indata, frames, time_info, status):
        if status:
            print(f"Audio recording status warning: {status}", file=sys.stderr)
        self.audio_queue.put(indata.copy())

    def load_whisper_model(self):
        try:
            from faster_whisper import WhisperModel
            model_size = self.config.get("model_size", "base")
            # Run model loading on CPU, int8 for maximum speed/memory efficiency on average machines.
            self.model = WhisperModel(model_size, device="cpu", compute_type="int8")
            self.model_loaded.set()
        except Exception as e:
            self.model_load_error = e
            self.model_loaded.set()

    def update_timer(self):
        if self.recording:
            self.recording_duration = time.time() - self.start_time
            minutes = int(self.recording_duration // 60)
            seconds = int(self.recording_duration % 60)
            self.info_label.configure(text=f"{minutes:02d}:{seconds:02d}")
            self.root.after(200, self.update_timer)

    def animate_recording(self):
        if not self.recording:
            return
            
        self.canvas.delete("all")
        
        # Center of canvas
        cx, cy = 40, 40
        
        # Pulsing outer ring (glow)
        # Radius goes from 16 to 34
        self.pulse_radius += 0.8 * self.pulse_direction
        if self.pulse_radius >= 32:
            self.pulse_direction = -1
        elif self.pulse_radius <= 16:
            self.pulse_direction = 1
            
        # Draw background ring (semi-transparent simulator by using a dark red tone on dark bg)
        # Interpolate color based on size to fade out
        opacity_factor = int((32 - self.pulse_radius) / 16 * 80) + 20
        # Color hex: higher radius -> darker red
        red_val = int(40 + (self.pulse_radius - 16) * 4) # 40 to 104
        glow_color = f"#{red_val:02x}1515"
        
        self.canvas.create_oval(cx - self.pulse_radius, cy - self.pulse_radius, 
                                cx + self.pulse_radius, cy + self.pulse_radius, 
                                fill=glow_color, outline="")
                                
        # Draw central solid red recording dot
        self.canvas.create_oval(cx - 14, cy - 14, cx + 14, cy + 14, fill="#FF3B30", outline="")
        
        self.root.after(30, self.animate_recording)

    def animate_transcribing(self, angle=0):
        if self.recording or not hasattr(self, 'transcribing_active') or not self.transcribing_active:
            return
            
        self.canvas.delete("all")
        cx, cy = 40, 40
        
        # Draw rotating blue arc spinner
        self.canvas.create_arc(cx - 20, cy - 20, cx + 20, cy + 20, 
                               start=angle, extent=80, 
                               outline="#0A84FF", width=3, style="arc")
                               
        self.canvas.create_arc(cx - 20, cy - 20, cx + 20, cy + 20, 
                               start=(angle + 180) % 360, extent=80, 
                               outline="#0A84FF", width=3, style="arc")
        
        self.root.after(20, self.animate_transcribing, (angle + 8) % 360)

    def stop_recording(self):
        if not self.recording:
            return
        self.recording = False
        
        # Stop audio stream
        if hasattr(self, 'stream') and self.stream.active:
            self.stream.stop()
            self.stream.close()
            
        # Gather all recorded audio data from queue
        audio_chunks = []
        while not self.audio_queue.empty():
            audio_chunks.append(self.audio_queue.get())
            
        if len(audio_chunks) > 0:
            self.recorded_audio = np.concatenate(audio_chunks, axis=0).flatten()
        else:
            self.recorded_audio = np.array([], dtype='float32')

        # Switch GUI to Transcribing state
        self.transcribing_active = True
        self.status_label.configure(text="Verarbeite Gedanken...")
        self.info_label.configure(text="Transkription wird vorbereitet...")
        self.footer_label.configure(text="Bitte warten...")
        self.animate_transcribing()
        
        # Run transcription in a background thread to prevent UI freezing
        threading.Thread(target=self.transcribe_process, daemon=True).start()

    def cancel(self):
        self.recording = False
        if hasattr(self, 'stream') and self.stream.active:
            self.stream.stop()
            self.stream.close()
        self.root.destroy()
        sys.exit(0)

    def transcribe_process(self):
        try:
            # Step 1: Wait for Whisper model to finish loading if not done
            if not self.model_loaded.is_set():
                self.root.after(0, self.update_info_text, "Modell wird geladen...")
                self.model_loaded.wait()
                
            if self.model_load_error:
                raise self.model_load_error
                
            if self.recorded_audio is None or len(self.recorded_audio) < self.sample_rate * 0.5:
                # Less than 0.5s of recording
                self.root.after(0, self.show_error, "Zu kurz aufgenommen.")
                return

            self.root.after(0, self.update_info_text, "Transkribiere...")
            
            # Step 2: Perform transcription
            segments, info = self.model.transcribe(
                self.recorded_audio,
                language=self.config.get("language", "de"),
                initial_prompt=self.config.get("initial_prompt", ""),
                vad_filter=self.config.get("vad_filter", True)
            )
            
            transcribed_text = "".join([segment.text for segment in segments]).strip()
            
            if transcribed_text:
                # Copy result to clipboard
                pyperclip.copy(transcribed_text)
                self.root.after(0, self.show_success, transcribed_text)
            else:
                self.root.after(0, self.show_error, "Keine Sprache erkannt.")
                
        except Exception as e:
            self.root.after(0, self.show_error, f"Fehler bei Transkription:\n{e}")

    def update_info_text(self, text):
        self.info_label.configure(text=text)

    def show_success(self, text):
        self.transcribing_active = False
        self.canvas.delete("all")
        
        # Draw green checkmark icon
        cx, cy = 40, 40
        self.canvas.create_oval(cx - 20, cy - 20, cx + 20, cy + 20, fill="#30D158", outline="")
        self.canvas.create_line(cx - 10, cy, cx - 3, cy + 7, fill="#FFFFFF", width=3, capstyle="round")
        self.canvas.create_line(cx - 3, cy + 7, cx + 10, cy - 7, fill="#FFFFFF", width=3, capstyle="round")
        
        self.status_label.configure(text="In Zwischenablage kopiert!")
        
        # Display preview of text (truncated if long)
        preview_text = text
        if len(preview_text) > 40:
            preview_text = preview_text[:37] + "..."
        self.info_label.configure(text=f'"{preview_text}"', fg="#30D158")
        self.footer_label.configure(text="Schließen...")
        
        # Auto close window
        delay = self.config.get("auto_exit_delay_ms", 1500)
        self.root.after(delay, self.close_app)

    def show_error(self, error_message):
        self.transcribing_active = False
        self.canvas.delete("all")
        
        # Draw red warning/error cross icon
        cx, cy = 40, 40
        self.canvas.create_oval(cx - 20, cy - 20, cx + 20, cy + 20, fill="#FF453A", outline="")
        self.canvas.create_line(cx - 8, cy - 8, cx + 8, cy + 8, fill="#FFFFFF", width=3, capstyle="round")
        self.canvas.create_line(cx - 8, cy + 8, cx + 8, cy - 8, fill="#FFFFFF", width=3, capstyle="round")
        
        self.status_label.configure(text="Abgebrochen / Fehler")
        
        # Show short message
        lines = error_message.split("\n")
        short_err = lines[0]
        if len(short_err) > 45:
            short_err = short_err[:42] + "..."
        self.info_label.configure(text=short_err, fg="#FF453A")
        self.footer_label.configure(text="Fenster schließt sich...")
        
        # Wait longer for error reading (3 seconds)
        self.root.after(3000, self.close_app)

    def close_app(self):
        self.root.destroy()
        sys.exit(0)


if __name__ == "__main__":
    try:
        config = load_config()
        
        # Check for installer flag
        if len(sys.argv) > 1 and sys.argv[1] == "--install":
            install_shortcut(config)
            sys.exit(0)
            
        # Normal GUI App startup
        root = tk.Tk()
        app = DictationApp(root, config)
        root.mainloop()
    except Exception as e:
        import traceback
        log_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "crash_log.txt")
        try:
            with open(log_file, "w", encoding="utf-8") as f:
                f.write(f"Crash occurred at {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
                traceback.print_exc(file=f)
        except Exception as write_err:
            print(f"Failed to write crash log: {write_err}")
            
        try:
            import tkinter as tk
            from tkinter import messagebox
            r = tk.Tk()
            r.withdraw()
            messagebox.showerror("GedankenDiktat Fehler beim Starten", f"Ein Fehler ist aufgetreten:\n{e}\n\nDetails wurden in crash_log.txt gespeichert.")
            r.destroy()
        except:
            pass
        sys.exit(1)
