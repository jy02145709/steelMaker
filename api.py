from pathlib import Path
from tempfile import TemporaryDirectory

import ezdxf
from fastapi import FastAPI, HTTPException, UploadFile

from steelmaker import analyze_dxf

app = FastAPI(title="SteelMaker")
MAX_UPLOAD_BYTES = 20 * 1024 * 1024


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/analyze")
def analyze(file: UploadFile):
    if Path(file.filename or "").suffix.lower() != ".dxf":
        raise HTTPException(415, "DXF 파일이 필요합니다.")
    with TemporaryDirectory() as directory:
        path = Path(directory) / "drawing.dxf"
        with path.open("wb") as target:
            remaining = MAX_UPLOAD_BYTES
            while chunk := file.file.read(64 * 1024):
                remaining -= len(chunk)
                if remaining < 0:
                    raise HTTPException(413, "파일은 20 MiB 이하여야 합니다.")
                target.write(chunk)
        try:
            return analyze_dxf(path)
        except (ezdxf.DXFError, OSError, UnicodeError) as exc:
            raise HTTPException(422, "DXF 파일을 읽을 수 없습니다.") from exc
