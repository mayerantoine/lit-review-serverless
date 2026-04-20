import os
import logging
from pathlib import Path
from enum import Enum
from dotenv import load_dotenv

# Load .env file from project root (parent directory of api/)
env_path = Path(__file__).parent.parent / ".env"
load_dotenv(dotenv_path=env_path)

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)

# Create logger
logger = logging.getLogger(__name__)



class Config:
    """Application configuration from environment variables"""


    # OpenAI Configuration
    OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")


# Global config instance
config = Config()


# Export logger for use in other modules
def get_logger(name: str) -> logging.Logger:
    """Get a logger instance for a specific module"""
    return logging.getLogger(name)
