import sys, base64, json

def decode(json_path, out_path):
    with open(json_path) as f:
        data = json.load(f)
    text = data[0]["text"]
    if text.startswith('"') and text.endswith('"'):
        text = json.loads(text)
    b64 = text.split("base64,", 1)[1]
    with open(out_path, "wb") as f:
        f.write(base64.b64decode(b64))
    print(f"wrote {out_path}")

if __name__ == "__main__":
    decode(sys.argv[1], sys.argv[2])
