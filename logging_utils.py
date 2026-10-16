"""
Logging utilities for consistent logging across the TikTok Recorder system.
Provides standardized timestamp formats and severity levels.
"""

import sys
import logging
from datetime import datetime
from typing import Optional, Callable

# Configure default logger
def setup_logger(name: str, level: int = logging.INFO) -> logging.Logger:
    """
    Setup a logger with consistent formatting.
    
    Args:
        name: Logger name (usually __name__)
        level: Logging level (default: INFO)
        
    Returns:
        Configured logger instance
    """
    logger = logging.getLogger(name)
    
    # Avoid duplicate handlers
    if logger.handlers:
        return logger
    
    # Create formatter with timestamp
    formatter = logging.Formatter(
        '[%(asctime)s] [%(levelname)s] %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    
    # Console handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    
    logger.addHandler(console_handler)
    logger.setLevel(level)
    
    # Prevent propagation to root logger
    logger.propagate = False
    
    return logger

# Default logger for modules that don't need their own
_default_logger = setup_logger("tiktok_recorder")

def log_info(message: str, logger: Optional[logging.Logger] = None):
    """Log an informational message."""
    if logger is None:
        logger = _default_logger
    logger.info(message)

def log_warning(message: str, logger: Optional[logging.Logger] = None):
    """Log a warning message."""
    if logger is None:
        logger = _default_logger
    logger.warning(message)

def log_error(message: str, logger: Optional[logging.Logger] = None):
    """Log an error message."""
    if logger is None:
        logger = _default_logger
    logger.error(message)

def log_debug(message: str, logger: Optional[logging.Logger] = None):
    """Log a debug message."""
    if logger is None:
        logger = _default_logger
    logger.debug(message)

# Compatibility functions for existing code that uses print()
def print_with_timestamp(message: str):
    """Print message with timestamp (for backward compatibility)."""
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    print(f"[{timestamp}] {message}", flush=True)

def print_error(message: str):
    """Print error message with timestamp and ERROR label."""
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    print(f"[{timestamp}] [ERROR] {message}", flush=True)

def print_success(message: str):
    """Print success message with timestamp and SUCCESS label."""
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    print(f"[{timestamp}] [SUCCESS] {message}", flush=True)

# Context manager for timing operations
import time
from contextlib import contextmanager

@contextmanager
def timed_operation(operation_name: str, logger: Optional[logging.Logger] = None):
    """
    Context manager that logs the duration of an operation.
    
    Usage:
        with timed_operation("Process video"):
            # do work
    """
    start_time = time.time()
    if logger is None:
        logger = _default_logger
    
    logger.info(f"Starting: {operation_name}")
    
    try:
        yield
    finally:
        elapsed = time.time() - start_time
        logger.info(f"Completed: {operation_name} (took {elapsed:.2f}s)")

# Utility to get formatted timestamp string
def get_timestamp_string() -> str:
    """Get current timestamp in YYYY-MM-DD HH:MM:SS format."""
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')

def get_timestamp_filename() -> str:
    """Get current timestamp suitable for filenames (YYYY-MM-DD_HH-MM-SS)."""
    return datetime.now().strftime('%Y-%m-%d_%H-%M-%S')