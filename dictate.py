import os
import sys
import json
import queue
import threading
import time
import random
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
    "initial_prompt": "Diktat auf Deutsch, enthält Software-Entwicklungs-Begriffe wie: git, commit, branch, push, pull request, merge, frontend, backend, refactoring, API, bug, code, deploy, database, pipeline, function, class, variable, loop, array, json, python.",
    "silence_threshold": 0.015,
    "silence_duration_sec": 0.8,
    "max_chunk_duration_sec": 15.0
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
        self.lock = threading.Lock()
        
        # Setup GUI Window Properties
        self.root.title("GedankenDiktat")
        self.root.configure(bg="#0B0C0E") # Deeper dark color
        
        # Position in top-right corner of screen
        window_width = 460
        window_height = 240
        self.root.overrideredirect(True) # Make it borderless
        self.root.attributes("-topmost", True) # Keep on top
        
        screen_width = self.root.winfo_screenwidth()
        screen_height = self.root.winfo_screenheight()
        margin_x = 30
        margin_y = 50  # below the top edge
        x = screen_width - window_width - margin_x
        y = margin_y
        self.root.geometry(f"{window_width}x{window_height}+{x}+{y}")
        
        # Bind Drag events to allow moving the window
        self.root.bind("<Button-1>", self.start_drag)
        self.root.bind("<B1-Motion>", self.drag)
        
        # Audio recording & chunking variables
        self.sample_rate = 16000 # Whisper expects 16kHz
        self.recording = True
        self.recording_duration = 0.0
        
        # Visualizer variables
        self.current_volume = 0.0
        self.smooth_volume = 0.0
        
        # Dynamic chunking queues
        self.transcription_queue = queue.Queue()
        self.chunk_index = 0
        self.segment_texts = {} # dict mapping index -> text
        
        # Audio buffers for the current active chunk
        self.current_segment_chunks = []
        self.current_segment_length = 0 # in samples
        
        # Silence detection parameters from config
        self.silence_threshold = self.config.get("silence_threshold", 0.015)
        self.silence_limit_samples = int(self.config.get("silence_duration_sec", 0.8) * self.sample_rate)
        self.max_segment_samples = int(self.config.get("max_chunk_duration_sec", 15.0) * self.sample_rate)
        self.silence_samples_counter = 0
        
        # Track previous transcribed text to use as prompt context
        self.previous_transcribed_text = ""
        
        # Background Whisper model loading
        self.model = None
        self.model_loaded = threading.Event()
        self.model_load_error = None
        
        # Start background model loading
        self.model_thread = threading.Thread(target=self.load_whisper_model, daemon=True)
        self.model_thread.start()
        
        # Start background queue transcriber
        self.worker_thread = threading.Thread(target=self.transcription_queue_worker, daemon=True)
        self.worker_thread.start()
        
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
        self.animate_recording()

    def start_drag(self, event):
        self.drag_start_x = event.x
        self.drag_start_y = event.y

    def drag(self, event):
        x = self.root.winfo_x() + (event.x - self.drag_start_x)
        y = self.root.winfo_y() + (event.y - self.drag_start_y)
        self.root.geometry(f"+{x}+{y}")

    def setup_ui(self):
        # Outer border frame (sleek premium gray border)
        self.border_frame = tk.Frame(self.root, bg="#23252E", bd=1)
        self.border_frame.place(relx=0, rely=0, relwidth=1, relheight=1)
        
        # Inner content frame
        self.content_frame = tk.Frame(self.border_frame, bg="#0B0C0E")
        self.content_frame.place(relx=0.005, rely=0.01, relwidth=0.99, relheight=0.98)
        
        # Close button in the top right
        self.close_btn = tk.Label(self.content_frame, text="✕", fg="#6E6E73", bg="#0B0C0E", font=("Segoe UI", 11, "bold"), cursor="hand2")
        self.close_btn.place(x=420, y=10)
        self.close_btn.bind("<Button-1>", lambda e: self.cancel())
        self.close_btn.bind("<Enter>", lambda e: self.close_btn.configure(fg="#FF453A"))
        self.close_btn.bind("<Leave>", lambda e: self.close_btn.configure(fg="#6E6E73"))
        
        # Canvas for dynamic volume visualizer / processing spinner
        self.canvas = tk.Canvas(self.content_frame, width=120, height=80, bg="#0B0C0E", bd=0, highlightthickness=0)
        self.canvas.pack(pady=(25, 5))
        
        # Status Label
        self.status_label = tk.Label(self.content_frame, text="Gedanken aufnehmen...", fg="#FFFFFF", bg="#0B0C0E", font=("Segoe UI", 15, "bold"))
        self.status_label.pack(pady=5)
        
        # Timer / Info Label
        self.info_label = tk.Label(self.content_frame, text="00:00", fg="#8E8E93", bg="#0B0C0E", font=("Segoe UI", 11))
        self.info_label.pack()
        
        # Shortcuts / Help footer
        self.footer_label = tk.Label(
            self.content_frame, 
            text="[Leertaste / Enter] Fertig  |  [Esc] Abbrechen", 
            fg="#545458", 
            bg="#0B0C0E", 
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
        
        if not self.recording:
            return
            
        with self.lock:
            chunk_copy = indata.copy()
            self.current_segment_chunks.append(chunk_copy)
            self.current_segment_length += len(chunk_copy)
            
            # Calculate volume level (RMS)
            rms = np.sqrt(np.mean(chunk_copy**2)) if len(chunk_copy) > 0 else 0.0
            self.current_volume = rms
            
            if rms < self.silence_threshold:
                self.silence_samples_counter += len(chunk_copy)
            else:
                self.silence_samples_counter = 0
                
            # Check cut conditions
            silence_triggered = (self.silence_samples_counter >= self.silence_limit_samples) and (self.current_segment_length >= 3.0 * self.sample_rate)
            length_triggered = self.current_segment_length >= self.max_segment_samples
            
            if silence_triggered or length_triggered:
                self.push_current_segment()

    def push_current_segment(self):
        if not self.current_segment_chunks:
            return
            
        # Concatenate audio chunks
        segment_audio = np.concatenate(self.current_segment_chunks, axis=0).flatten()
        
        # Reset counters
        self.current_segment_chunks = []
        self.current_segment_length = 0
        self.silence_samples_counter = 0
        
        if len(segment_audio) > 0:
            idx = self.chunk_index
            self.chunk_index += 1
            self.transcription_queue.put((idx, segment_audio))

    def load_whisper_model(self):
        try:
            from faster_whisper import WhisperModel
            model_size = self.config.get("model_size", "small")
            self.model = WhisperModel(model_size, device="cpu", compute_type="int8")
            self.model_loaded.set()
        except Exception as e:
            self.model_load_error = e
            self.model_loaded.set()

    def transcription_queue_worker(self):
        while True:
            item = self.transcription_queue.get()
            if item is None:
                break
                
            idx, audio_data = item
            
            try:
                # Wait for model load
                if not self.model_loaded.is_set():
                    self.model_loaded.wait()
                    
                if self.model_load_error:
                    raise self.model_load_error
                
                # Context prompting
                base_prompt = self.config.get("initial_prompt", "")
                if self.previous_transcribed_text:
                    combined_prompt = f"{base_prompt} Previously: {self.previous_transcribed_text}"
                else:
                    combined_prompt = base_prompt
                
                segments, info = self.model.transcribe(
                    audio_data,
                    language=self.config.get("language", "de"),
                    initial_prompt=combined_prompt,
                    vad_filter=self.config.get("vad_filter", True)
                )
                text = "".join([segment.text for segment in segments]).strip()
                self.segment_texts[idx] = text
                
                if text:
                    self.previous_transcribed_text = text
                    
            except Exception as e:
                print(f"Error transcribing chunk {idx}: {e}", file=sys.stderr)
                self.segment_texts[idx] = ""
                
            self.transcription_queue.task_done()

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
        
        # Smooth volume transition
        self.smooth_volume = self.smooth_volume * 0.7 + self.current_volume * 0.3
        
        # Canvas dimensions are 120x80. Center is (60, 40)
        cx, cy = 60, 40
        
        # 1. Glowing red backdrop pulsing with volume
        glow_r = 18 + min(self.smooth_volume * 150, 22)
        self.canvas.create_oval(cx - glow_r, cy - glow_r, cx + glow_r, cy + glow_r, 
                                fill="#2A0B0F", outline="")
        
        # 2. Draw 7 dancing equalizer bars
        # Total width: 7 bars of 6px with 4px gaps = 66px
        x_start = cx - 33
        bar_width = 6
        gap = 4
        
        base_h = 6
        max_h = 44
        
        # Center bars respond more dynamically than side bars
        multipliers = [0.4, 0.7, 1.1, 1.4, 1.1, 0.7, 0.4]
        
        for i in range(7):
            # Scale volume RMS (typically 0.0 - 0.1 for speech, max out around 0.15)
            volume_scale = min(self.smooth_volume * 180, 1.0)
            
            # Subtle random flicker to keep visualizer "alive" even when silent
            flicker = random.uniform(0.0, 1.5)
            h = base_h + (volume_scale * multipliers[i] * (max_h - base_h)) + flicker
            h = min(h, max_h)
            
            # Draw line with rounded cap to form a pill shape
            x = x_start + i * (bar_width + gap) + (bar_width / 2)
            y1 = cy - (h / 2)
            y2 = cy + (h / 2)
            
            # Color is a sleek gradient: base red #FF375F, top orange #FF9F0A
            # We can use modern vibrant coral red (#FF453A)
            self.canvas.create_line(x, y1, x, y2, fill="#FF453A", width=bar_width, capstyle="round")
                                    
        self.root.after(30, self.animate_recording)

    def animate_transcribing(self, angle=0):
        if self.recording or not hasattr(self, 'transcribing_active') or not self.transcribing_active:
            return
            
        self.canvas.delete("all")
        cx, cy = 60, 40
        
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
        
        # Stop stream
        if hasattr(self, 'stream') and self.stream.active:
            self.stream.stop()
            self.stream.close()
            
        # Push remaining audio buffer
        with self.lock:
            if self.current_segment_chunks:
                self.push_current_segment()
                
        # Send shutdown sentinel
        self.transcription_queue.put(None)

        # Switch GUI to Transcribing state
        self.transcribing_active = True
        self.status_label.configure(text="Verarbeite Gedanken...")
        self.info_label.configure(text="Transkription wird abgeschlossen...")
        self.footer_label.configure(text="Bitte warten...")
        self.animate_transcribing()
        
        # Finish transcription in background thread
        threading.Thread(target=self.finish_transcription, daemon=True).start()

    def cancel(self):
        self.recording = False
        if hasattr(self, 'stream') and self.stream.active:
            self.stream.stop()
            self.stream.close()
        self.root.destroy()
        sys.exit(0)

    def finish_transcription(self):
        try:
            self.worker_thread.join()
            
            ordered_texts = []
            for i in range(self.chunk_index):
                text = self.segment_texts.get(i, "")
                if text:
                    ordered_texts.append(text)
                    
            final_text = " ".join(ordered_texts).strip()
            final_text = " ".join(final_text.split())
            
            if final_text:
                pyperclip.copy(final_text)
                self.root.after(0, self.show_success, final_text)
            else:
                self.root.after(0, self.show_error, "Keine Sprache erkannt.")
                
        except Exception as e:
            self.root.after(0, self.show_error, f"Fehler bei Transkription:\n{e}")

    def update_info_text(self, text):
        self.info_label.configure(text=text)

    def show_success(self, text):
        self.transcribing_active = False
        self.canvas.delete("all")
        
        cx, cy = 60, 40
        self.canvas.create_oval(cx - 20, cy - 20, cx + 20, cy + 20, fill="#30D158", outline="")
        self.canvas.create_line(cx - 10, cy, cx - 3, cy + 7, fill="#FFFFFF", width=3, capstyle="round")
        self.canvas.create_line(cx - 3, cy + 7, cx + 10, cy - 7, fill="#FFFFFF", width=3, capstyle="round")
        
        self.status_label.configure(text="In Zwischenablage kopiert!")
        
        preview_text = text
        if len(preview_text) > 40:
            preview_text = preview_text[:37] + "..."
        self.info_label.configure(text=f'"{preview_text}"', fg="#30D158")
        self.footer_label.configure(text="Schließen...")
        
        delay = self.config.get("auto_exit_delay_ms", 1500)
        self.root.after(delay, self.close_app)

    def show_error(self, error_message):
        self.transcribing_active = False
        self.canvas.delete("all")
        
        cx, cy = 60, 40
        self.canvas.create_oval(cx - 20, cy - 20, cx + 20, cy + 20, fill="#FF453A", outline="")
        self.canvas.create_line(cx - 8, cy - 8, cx + 8, cy + 8, fill="#FFFFFF", width=3, capstyle="round")
        self.canvas.create_line(cx - 8, cy + 8, cx + 8, cy - 8, fill="#FFFFFF", width=3, capstyle="round")
        
        self.status_label.configure(text="Abgebrochen / Fehler")
        
        lines = error_message.split("\n")
        short_err = lines[0]
        if len(short_err) > 45:
            short_err = short_err[:42] + "..."
        self.info_label.configure(text=short_err, fg="#FF453A")
        self.footer_label.configure(text="Fenster schließt sich...")
        
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
