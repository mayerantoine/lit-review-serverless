import os
import logging
from enum import Enum

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

    # OpenAI Configuration — injected by Lambda runtime or agent container env
    OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")


# Global config instance
config = Config()


# Export logger for use in other modules
def get_logger(name: str) -> logging.Logger:
    """Get a logger instance for a specific module"""
    return logging.getLogger(name)
