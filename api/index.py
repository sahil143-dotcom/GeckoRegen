# api/index.py
# Vercel serverless function entry point

from server import app

# Expose Flask app as 'handler' for Vercel
handler = app