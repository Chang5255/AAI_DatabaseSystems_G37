import subprocess
import sys
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[2]

scripts = [
    ROOT_DIR / "database" / "mongodb" / "create_collections.py",
    ROOT_DIR / "database" / "mongodb" / "import_movie_features.py",
    ROOT_DIR / "database" / "mongodb" / "create_indexes.py",
]


def run_script(script):
    print("\n" + "=" * 60)
    print(f"Running: {script.name}")
    print("=" * 60)

    result = subprocess.run(
        [sys.executable, str(script)]
    )

    if result.returncode != 0:
        print(
            f"\nSetup failed while running "
            f"{script.name}"
        )
        sys.exit(result.returncode)


if __name__ == "__main__":

    print("\nMongoDB Project Setup")
    print("=" * 60)

    for script in scripts:
        run_script(script)

    print("\n" + "=" * 60)
    print("MongoDB setup completed successfully.")
    print("=" * 60)