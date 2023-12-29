import os
from dotenv import load_dotenv
from typing import get_type_hints

# load environment variables from .env file
load_dotenv()


class ConfigError(Exception):
    pass


class Config:
    DISCOGS_USER: str
    TOKEN: str
    FEED_URL: str
    FEED_AUTHOR_NAME: str
    FEED_AUTHOR_EMAIL: str
    TIMEOUT: float = 30.0
    MAX_FEED_ENTRIES: int = 50
    FEED_DISPLAY_WIDTH: int = 28
    STANDARD_SHIPPING: float = 5.00

    """
    Map environment variables to class fields according to these rules:
      - Field won't be parsed unless it has a type annotation
      - Field will be skipped if not in all caps
      - Class field and environment variable name are the same
    """

    def __init__(self, env):
        # annotations = inspect.get_annotations(Config)  python 3.10 and up
        annotations = Config.__annotations__
        for field in annotations:
            if not field.isupper():
                continue

            default_value = getattr(self, field, None)
            if default_value is None and env.get(field) is None:
                raise ConfigError(f"The {field} field is required")

            var_type = get_type_hints(Config)[field]
            raw_value = env.get(field, default_value)

            try:
                if var_type == str:
                    value = str(raw_value.strip("'"))
                else:
                    value = var_type(raw_value)

                setattr(self, field, value)

            except ValueError as e:
                raise ConfigError(
                    'Unable to cast value of "{}" to type "{}" for "{}" field'.format(
                        raw_value, var_type, field
                    )
                ) from e

    def __repr__(self):
        return str(self.__dict__)


config = Config(os.environ)
