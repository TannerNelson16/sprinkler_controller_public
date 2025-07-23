# Intellidwell Sprinkler Controller Firmware

This repository contains the open-source firmware for the **Intellidwell ESP32-Based Smart Sprinkler Controller**, developed by Tanner Nelson. It is designed to run on ESP32 microcontrollers and provides an intuitive web-based interface, scheduling features, MQTT integration, and support for multiple zones and rain delay logic.

This code powers the official Intellidwell controller hardware, which is available for purchase. It is released for educational purposes, hobbyists, and developers who wish to contribute to or adapt the project for **personal** and **non-commercial** use.

---

## 🚫 Licensing and Usage

This project is licensed under the [GNU General Public License v3 (GPLv3)](https://www.gnu.org/licenses/gpl-3.0.en.html), **with additional restrictions on commercial use of this code in hardware products**.

### In Plain English:
- ✅ You can view, modify, and run the code for personal projects.
- ✅ You can fork the project and share improvements (as long as they remain open-source).
- ❌ You may **not** manufacture, sell, or distribute hardware products that use this code or derivatives without express written permission from the author.

The intent is to support open development and tinkering while protecting against commercial knockoffs.

If you're interested in commercial use, please contact Tanner Nelson at [tanner@intellidwell.net](mailto:tanner@intellidwell.net) to discuss licensing.

---

## 🔧 Features
- Web-based control panel hosted directly on the ESP32
- Per-zone scheduling with customizable run times
- MQTT support for automation and remote control
- NTP time synchronization with time zone selection
- Rain delay and disable logic
- JSON-based configuration persistence
- Local network fallback to AP mode if Wi-Fi fails

---

## 📦 Hardware Requirements
This repo only includes firmware. No PCB design files or hardware schematics are included. To purchase the official hardware, visit [https://intellidwell.net](https://intellidwell.net).

---

## 📄 License

This project is licensed under GPLv3 with an additional **non-commercial hardware restriction**. See the [LICENSE](./LICENSE) file for full terms.


