"""
INF2003 Group Project - Team 37
Run the web UI:  python app.py   then open http://127.0.0.1:5000
"""

from webapp import create_app

app = create_app()

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=True)
