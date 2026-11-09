import re


def normalize(text):
    return re.sub(r"[^a-z0-9一-鿿]", "", text.lower())


def grams(text):
    text = normalize(text)
    return {text[i:i+2] for i in range(max(0, len(text)-1))}
