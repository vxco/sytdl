import os
import sys
import subprocess
import platform


def check_dependencies():
    try:
        import PyInstaller
    except ImportError:
        print("Installing PyInstaller...")
        subprocess.check_call([sys.executable, "-m", "pip", "install", "pyinstaller"])


def bundle_application():
    system = platform.system().lower()

    # Base PyInstaller command
    cmd = [
        "pyinstaller",
        "--onefile",
        "--windowed",
        "--clean",
        "--name", "SYTDL",
        "--add-data", "settings.json;." if system == "windows" else "settings.json:.",
        "--add-data", "download_history.json;." if system == "windows" else "download_history.json:."
    ]

    # Platform specific options
    if system == "windows":
        cmd.extend([
            "--icon=icon.ico",
            "--version-file=version.txt"
        ])
    elif system == "darwin":
        cmd.extend([
            "--icon=icon.icns",
            "--osx-bundle-identifier=com.simit.sytdl"
        ])
    elif system == "linux":
        cmd.extend([
            "--icon=icon.png"
        ])

    # Add the main script
    cmd.append("main.py")

    try:
        # Create empty settings and history files if they don't exist
        if not os.path.exists("settings.json"):
            with open("settings.json", "w") as f:
                f.write("{}")

        if not os.path.exists("download_history.json"):
            with open("download_history.json", "w") as f:
                f.write("[]")

        # Run PyInstaller
        print(f"Building application for {system}...")
        subprocess.check_call(cmd)

        print("\nBuild completed successfully!")
        print(f"Your executable can be found in the 'dist' folder")

        # Additional platform-specific instructions
        if system == "darwin":
            print("Note: On macOS, you may need to:")
            print("1. Right-click the app and select 'Open' the first time")
            print("2. Go to System Preferences > Security & Privacy to allow the app to run")
        elif system == "linux":
            print("Note: On Linux, you may need to:")
            print("1. Make the file executable: chmod +x ./dist/SYTDL")
            print("2. Run it from terminal first time: ./dist/SYTDL")

    except subprocess.CalledProcessError as e:
        print(f"Error during build process: {e}")
        sys.exit(1)


def main():
    print("=== SYTDL Application Bundler ===")

    # Check and install dependencies
    check_dependencies()

    # Bundle the application
    bundle_application()


if __name__ == "__main__":
    main()
