
import subprocess
import sys
import urllib.request
from pathlib import Path

SCRIPT_URL = (
    "https://raw.githubusercontent.com/its-rach/"
    "fliphtml5Downloader/main/fliphtml5Downloader.py"
)

CARPETA = Path(__file__).resolve().parent
SCRIPT = CARPETA / "fliphtml5Downloader.py"


def preparar():
    if not SCRIPT.exists():
        print("Descargando el motor de descarga...")
        urllib.request.urlretrieve(SCRIPT_URL, SCRIPT)

    paquetes = ["requests", "img2pdf", "Pillow", "wasmtime"]

    subprocess.check_call([
        sys.executable, "-m", "pip",
        "install", *paquetes
    ])


def main():
    print("=" * 45)
    print("      DESCARGADOR DE FLIPHTML5")
    print("=" * 45)

    url = input("\nPega la URL de FlipHTML5: ").strip()

    if not url:
        print("No introdujiste ninguna URL.")
        input("Presiona Enter para salir...")
        return

    if "fliphtml5.com/" not in url:
        print("La URL no parece ser de FlipHTML5.")
        input("Presiona Enter para salir...")
        return

    try:
        preparar()

        print("\nIniciando descarga...\n")

        subprocess.run(
            [sys.executable, str(SCRIPT), url],
            cwd=str(CARPETA),
            check=True
        )

        print("\nProceso terminado.")

    except subprocess.CalledProcessError as error:
        print(f"\nEl descargador terminó con un error: {error}")

    except Exception as error:
        print(f"\nNo se pudo completar la operación: {error}")

    input("\nPresiona Enter para cerrar...")


if __name__ == "__main__":
    main()
