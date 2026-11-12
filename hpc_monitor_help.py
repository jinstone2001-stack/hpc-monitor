"""Read the same offline help used by the standalone HTML and Python web UI."""
import json
from pathlib import Path


def help_text():
    html = Path(__file__).with_name("HPC_Usage_Monitor.html").read_text(encoding="utf-8")
    sections = json.loads(html.split('<script id="hpc-help" type="application/json">', 1)[1].split('</script>', 1)[0])
    return "\n\n".join(section["title"] + "\n" + "\n\n".join(section["paragraphs"]) for section in sections)
