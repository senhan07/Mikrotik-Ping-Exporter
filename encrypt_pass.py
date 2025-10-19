#!/usr/bin/env python3
import getpass
from cryptography.fernet import Fernet

def generate_key():
    """Generates a key and saves it to a file."""
    key = Fernet.generate_key()
    with open("secret.key", "wb") as key_file:
        key_file.write(key)
    print("Encryption key saved to secret.key")
    return key

def encrypt_password(key):
    """Encrypts a password using the provided key."""
    password = getpass.getpass("Enter password to encrypt: ").encode()
    f = Fernet(key)
    encrypted_password = f.encrypt(password)
    print("\nEncrypted password:")
    print(encrypted_password.decode())

if __name__ == "__main__":
    try:
        with open("secret.key", "rb") as key_file:
            key = key_file.read()
    except FileNotFoundError:
        print("No secret key found. Generating a new one.")
        key = generate_key()

    encrypt_password(key)
