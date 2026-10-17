from pymongo import MongoClient
from dotenv import load_dotenv
import os

load_dotenv()

mongo_uri = os.getenv("MONGODB_URI")
database_name = os.getenv("MONGODB_DATABASE")

client = MongoClient(mongo_uri)

db = client[database_name]

try:
    client.admin.command("ping")
    print("MongoDB connection successful.")
    print(f"Connected to database: {database_name}")

except Exception as e:
    print("MongoDB connection failed.")
    print(e)