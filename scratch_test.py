import os
from dotenv import dotenv_values
env_path = ".env"
parsed = dotenv_values(env_path)
print("KEYS:")
for k in parsed.keys():
    print(repr(k), "=>", repr(parsed[k]))
