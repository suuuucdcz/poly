import uvicorn
import webbrowser
import threading
import time
import os
import sys

def open_browser():
    # Wait for the FastAPI server to start
    time.sleep(1.5)
    url = "http://127.0.0.1:8000"
    print(f"\n[LAUNCHER] Démarrage du tableau de bord à l'adresse: {url}")
    print("[LAUNCHER] Ouverture du navigateur par défaut...")
    webbrowser.open(url)

if __name__ == "__main__":
    # Ensure correct working directory is the workspace root
    script_dir = os.path.dirname(os.path.abspath(__file__))
    os.chdir(script_dir)
    
    # Start the browser opener in a daemon thread
    browser_thread = threading.Thread(target=open_browser, daemon=True)
    browser_thread.start()
    
    # Run the uvicorn server programmatically
    print("[LAUNCHER] Lancement du serveur FastAPI / Uvicorn...")
    try:
        uvicorn.run("backend.main:app", host="127.0.0.1", port=8000, log_level="info")
    except KeyboardInterrupt:
        print("\n[LAUNCHER] Serveur arrêté par l'utilisateur.")
    except Exception as e:
        print(f"\n[LAUNCHER] Erreur fatale lors du démarrage du serveur: {e}")
        sys.exit(1)
