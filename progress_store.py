current_status = "대기 중..."

def set_status(msg: str):
    global current_status
    current_status = msg

def get_status() -> str:
    return current_status
