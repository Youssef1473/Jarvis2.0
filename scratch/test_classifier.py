import re

def classify(q):
    ql = q.lower().strip()
    if any(t in ql for t in ['screen', 'look at', 'screenshot']):
        return 'deep (vision)'
    deep_indicators = [
        'code', 'python', 'script', 'debug', 'refactor', 'git', 'function',
        'write an email', 'draft', 'compose', 'summarize', 'analyze',
        'explain in detail', 'plan', 'research', 'compare', 'why does',
        'how do i implement', 'architecture', 'solve', 'math', 'calculate',
        'review', 'investigate', 'audit'
    ]
    if any(ql.startswith(p) for p in ['why ', 'how come ', 'explain ', 'what causes ', 'compare ', 'analyze ']):
        return 'deep'
    if any(ind in ql for ind in deep_indicators):
        return 'deep'
    if len(ql.split()) > 18 or len(re.findall(r'[.!?]', ql)) > 1:
        return 'deep'
    fast_indicators = [
        'volume', 'mute', 'unmute', 'sound', 'louder', 'quieter',
        'battery', 'charge', 'power', 'time', 'date', 'day', 'clock',
        'open', 'launch', 'start', 'close', 'kill', 'exit',
        'play', 'pause', 'skip', 'next song', 'previous song',
        'hello', 'hi', 'hey', 'good morning', 'good evening', 'good afternoon',
        'how are you', 'who are you', 'status', 'are you there',
        'thank you', 'thanks'
    ]
    if any(ind in ql for ind in fast_indicators):
        return 'fast'
    return 'deep'

test_queries = [
    'Jarvis, set volume to 50',
    'Open Spotify for me',
    'What is my battery level?',
    'What time is it in London?',
    'Mute audio',
    'Good morning Jarvis, are you online?',
    'Pause the music',
    'Launch Chrome',
    'Explain how transformers work in neural networks',
    'Debug this Python function for me',
    'Look at my screen and tell me what error this is',
    'Draft an email to John about the project roadmap',
    'Summarize our meeting notes from today',
    'Why is my memory usage high?',
    'Can you write a bash script to backup my sqlite database?'
]

for q in test_queries:
    print(f'{classify(q).upper():<15} -> "{q}"')
