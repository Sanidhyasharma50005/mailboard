# Mailboard ✉️

Mailboard sorts your Gmail inbox into colourful category columns on one single screen with no vertical scrolling.

---

## For Users

1. **Download & Run**:
   - Download `Mailboard.exe` (from releases or your build).
   - Double-click `Mailboard.exe` to launch.
   - If Windows SmartScreen displays *"Windows protected your PC"*, click **More info** and then click **Run anyway**.
   - Your default web browser will open automatically to Mailboard.

2. **Connect Your Gmail**:
   - Mailboard starts in **DEMO mode** with 20 sample emails so you can explore immediately.
   - Click the **⚡ Connect Gmail** button in the top navigation bar or banner.
   - Follow the 3 simple steps in the modal:
     1. **Turn on 2-Step Verification** on your Google Account: [myaccount.google.com/security](https://myaccount.google.com/security).
     2. **Create an App Password**: visit [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords), name it "Mailboard", and generate a 16-character password.
     3. Enter your Gmail address and the 16-character password, then click **Connect**.
   - *Note:* Google App Passwords may be unavailable on some college or managed corporate Google Workspace accounts.
   - Your credentials are encrypted and stored safely in **Windows Credential Manager** (never plain text).

3. **Safe & Read-Only**:
   - Mailboard uses read-only IMAP (`BODY.PEEK`). It **never** deletes, moves, marks, or alters anything in your real Gmail inbox.
   - Click **🔌 Disconnect** at any time to remove saved credentials and return to demo mode.
   - Click **✖ Quit** in the top navigation bar to cleanly shut down Mailboard.

---

## For Developers

### Running from Source
1. **Install Python 3.10+** and install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

2. **Run Mailboard**:
   ```bash
   python app.py
   ```
   Mailboard will bind to `127.0.0.1:5000` (or `5001`-`5010` if busy), launch the browser automatically, and serve the application.
   *(Optional developer fallback: you can create a `.env` file with `GMAIL_ADDRESS` and `GMAIL_APP_PASSWORD`)*.

### Building the Windows Executable
Run the automated build script:
```cmd
build.bat
```
This will:
- Create a virtual environment `.venv`
- Install all requirements including PyInstaller and keyring
- Package a standalone single-file windowed executable `dist\Mailboard.exe`

Data, database (`mailboard.db`), and logs (`mailboard.log`) are stored in `%APPDATA%\Mailboard` (never alongside the `.exe`).
