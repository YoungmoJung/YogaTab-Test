import os
import warnings
from litellm import completion

#짧은 이름 -> 실제 모델 ID (모델 변경시 여기만 수정)
MODELS = {
    # Flash 계열 503(과부하) 지속으로 임시로 Flash-Lite 사용. 복구되면 "gemini/gemini-3.8-flash"로 되돌리기
    "gemini": "gemini/gemini-3.5-flash-lite",
    "claude": "anthropic/claude-haiku-5-5",
    "claude-pro": "anthropic/claude-sonnet-5-5",
    "Qwen": "groq/qwen/qwen3.8-27b",
}

# 모델별 추가 옵션 (MODELS의 짧은 이름 기준)
# Qwen: 추론(thinking) 출력을 끄고 바로 답하도록 설정
MODEL_OPTIONS = {
    "Qwen": {"extra_body": {"reasoning_effort": "none"}},
}

DEFAULT = os.getenv("LLM_MODEL","claude")

def canonical(model=None):
    """짧은 이름을 대소문자 구분 없이 MODELS의 키로 맞춤 (예: "qwen" -> "Qwen")
    MODELS에 없는 이름(실제 모델 ID 등)은 그대로 반환"""
    name = model or DEFAULT
    for key in MODELS:
        if key.lower() == name.lower():
            return key
    return name


def resolve(model=None):
    name = canonical(model)
    return MODELS.get(name, name)


def options(model=None):
    """모델별 추가 옵션 (대소문자 구분 없음). 호출 쪽에서 수정해도 원본이 바뀌지 않도록 복사본 반환"""
    return dict(MODEL_OPTIONS.get(canonical(model), {}))

#def chat(prompt, system=None, model=None, temperature=0):
#    messages=[]
#    if system:
#        messages.append({"role":"system", "content":system})
#    messages.append({"role":"user", "content":prompt})
#    resp = completion(model=resolve(model), messages=messages, temperature=temperature)
#    return resp.choices[0].message.content 


def chat(prompt, system=None, model=None, temperature=None, max_tokens=None, timeout=60,
         json_mode=False):
    """max_tokens: 최대 출력 토큰 수 (None이면 LiteLLM/모델 기본값)
    timeout: 1회 요청 제한 시간(초). 출력이 길면 함께 늘려야 함
    json_mode: True면 문법에 맞는 JSON만 출력하도록 제한 (response_format)"""
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    kwargs = options(model)
    if temperature is not None:
        kwargs["temperature"] = temperature
    if max_tokens is not None:
        kwargs["max_tokens"] = max_tokens
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}
    # 503 등 일시적 오류는 LiteLLM이 최대 3번 재시도
    resp = completion(model=resolve(model), messages=messages, **kwargs,
                      timeout=timeout, num_retries=3)
    choice = resp.choices[0]
    if choice.finish_reason == "length":
        warnings.warn(f"{resolve(model)} 응답이 max_tokens 한도에서 잘렸습니다. "
                      f"max_tokens 값을 늘려주세요.")
    return choice.message.content


def check_all():
    """등록된 모든 모델의 연결 상태 확인"""
    for name in MODELS:
        try:
            chat("OK 라고 답해줘.", model=name)
            print(f"[정상] {name} -> {MODELS[name]}")
        except Exception as e:
            print(f"[오류] {name}: {type(e).__name__} - {str(e)[:120]}")