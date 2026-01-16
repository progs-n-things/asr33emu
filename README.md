**ASR-33 terminal with paper tape reader/punch emulator.**

![screenshot](screenshot.png)

**Features:**

* Refactored and expanded (with the help of AI) version of Hugh Pyle's ttyemu project
* Supports Pygame and Tkinter frontends.
* Backends for serial and ssh (Paramiko library)
    Ssh has not been well tested, so use with caution.
* F1/F2 displays/hides the paper tape reader widget (written in Tkinter, but also works with the Pygame frontend)
* F3/F4 displays/hides the paper tape punch.
* By default, output is limited to an authentic 10 characters per second. Hit F5 to unthrottle the speed.
* Hit F6 to mute the sound.
* Sound is generated using Hugh Pyle's ASR-33 sound recording and Pygame mixer. The sound module Sound now works with both Tkinter and Pygame frontends. If it's too loud, hit F7 to close the lid.
* Hit F8 to switch between Line and Local modes.
* Hit F9 to turn the printer output on and off.
* Scrolling (with page up, down, home, end, mouse scroll - Tkinter frontend has a scrollbar)
* Tkinter and Pygame frontends can be launched from their modules (primarily for testing) or use the wrapper module to choose frontend/backend combinations and configurations. Supports yaml and command line parameters.
* New Teletype33.ttf font that includes lower case letters in the ASR-33 style (useful when connected to modern-day computers)

***

**Installation Notes**

**Windows:**
Install Python3 from the Microsoft Store or Python.org website.<br>
(Tested using Python 3.13 on Windows 11 Home, version 25H2)<br>

Python package installation is required to run asr33emu on Windows:<br>
On Windows using PowerShell or from a Command Prompt:<br>
* python3 -m pip install PyYAML
* python3 -m pip install pyserial
* python3 -m pip install paramiko
* python3 -m pip install pygame
* python3 -m pip install pillow
* python3 -m pip install fonttools

**Ubuntu**: (Tested on 24.04.3)<br>
Python package installation is required to run asr33emu on Ubuntu Linux:<br>
* sudo apt install python3-tk
* sudo apt install python3-pygame
* sudo apt install python3-pil.imagetk
* sudo apt install python3-fonttools
* sudo apt install python3-paramiko

Using serial ports in Ubuntu requires adding yourself to the "dialout" group:<br>
sudo usermod -a -G dialout $USER<br>
Then reboot (logout/login seems to be insufficient)<br>

**Running the asr33emu Terminal Emulator**<br>
Run asr33emu from the project directory by typing:<br>
    python3 ./asr33emu.py<br>
This will start up the emulator using the configuration defined in asr33_config.yaml<br>
Note that some config file parameters can be overridden on the command line or an alternate configuration file can be specified using "--config filename.yaml". Type: python3 ./asr33emu.py --help for more start up information.

***

**PiDP8/I Useage Notes**<br>
For the paper tape reader to load binary tapes, SIMH 8-bit terminal mode is required. In your boot script, ensure the SIMH emulator Terminal Input (TTI) device is set to operate in 8-bit mode by including the command:<br>
set tti 8b<br>

Some PDP8 programs expect the keyboard to send mark-parity (bit-7 set to 1). This results in non-standard ASCII characters being sent but seems to be necessary for some OS8 programs. I found FOCAL-69 also requires keyboard mark-parity. An ASR-33 printer seems to ignore the parity bit. This feature can be enabled in the configuration file, asr33_config.yaml, by setting keyboard_parity_mode to "mark". It should be set to "space" to generate standard ASCII characters.<br>

For most PDP8 communications you will also want keyboard_uppercase_only set to "true".<br>

I find it quite convenient to load both the RIM and Binary loaders in the same PiDP8 start up configuration file. I use "1.script" for this purpose. If you're looking for a more realistic experience, you can use the front panel to load the RIM loader and asr33emu's simulated paper tape reader to load the binary loader and the front panel to start it running.<br>

The self-starting EDU20C BASIC paper tape loads and runs fine using asr33emu. It requires only the RIM loader to load and at 10 characters per second, it takes about 25 minutes to load and start. If you're impatient, you can turn off the data rate throttle in the emulator, and it will load in a few minutes at a rate of about 300 CPS. ASR-33 teletypes connected to PDP8's typically had a hardware tape-reader auto-start/stop feature. This feature cannot be easily simulated, but an auto-stop has been added to the reader. It works by detecting trailing 200 octal or null characters. If auto-stop is not enabled, when EDU20C BASIC auto-starts, the tape reader will feed trailer bytes to its startup dialog resulting in garbage being entered at the setup prompts.<br>

