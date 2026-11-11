import os
from litellm import completion

#짧은 이름 -> 실제 모델 ID (모델 변경시 여기만 수정)
MODELS = {
    "gemini": "gemini/gemini-flash-latest",
    "claude": "anthropic/claude-haiku-5-5",
    "claude-pro": "anthropic/claude-sonnet-5-5",
}
DEFAULT = os.getenv("LLM_MODEL","claude")

def resolve(model=None):
    name = model or DEFAULT
    return MODELS.get(name, name)

#def chat(prompt, system=None, model=None, temperature=0):
#    messages=[]
#    if system:
#        messages.append({"role":"system", "content":system})
#    messages.append({"role":"user", "content":prompt})
#    resp = completion(model=resolve(model), messages=messages, temperature=temperature)
#    return resp.choices[0].message.content 


def chat(prompt, system=None, model=None, temperature=None):
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    kwargs = {}
    if temperature is not None:
        kwargs["temperature"] = temperature
    resp = completion(model=resolve(model), messages=messages, **kwargs, timeout=30)
    return resp.choices[0].message.content


def check_all():
    """등록된 모든 모델의 연결 상태 확인"""
    for name in MODELS:
        try:
            chat("OK 라고 답해줘.", model=name)
            print(f"[정상] {name} -> {MODELS[name]}")
        except Exception as e:
            print(f"[오류] {name}: {type(e).__name__} - {str(e)[:120]}")