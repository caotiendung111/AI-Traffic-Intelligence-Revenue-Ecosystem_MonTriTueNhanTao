from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import Optional

class Settings(BaseSettings):
    """
    System configuration management using Pydantic Settings.
    Loads values from environment variables or .env file.
    """
    # Core Security
    SECRET_KEY: str = "AI_TRAFFIC_ULTRA_PREMIUM_SECURE_TOKEN_2026_LONG"
    ALGORITHM: str = "HS256"
    TOKEN_EXPIRE_H: int = 8
    
    # Database
    DATABASE_URL: str = "sqlite:///./traffic_monitoring.db"
    
    # Business Logic
    PRICE_PER_VEHICLE: int = 50000
    SYSTEM_NAME: str = "TrafficAI"
    VERSION: str = "6.1.0"
    
    # CORS
    ALLOWED_ORIGINS: list[str] = ["http://localhost:8001", "http://127.0.0.1:8001"]
    
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

# Global settings instance
settings = Settings()
