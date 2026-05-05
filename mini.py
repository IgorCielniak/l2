import sys

stack = []
words = {}

if len(sys.argv) < 2:
    sys.exit("usage: python mini.py program.sl")

s = open(sys.argv[1]).read()

tokens = []
i = 0
n = len(s)
while i < n:
    if s[i].isspace():
        i += 1
        continue
    if s[i] == '"':
        i += 1
        start = i
        while i < n and s[i] != '"':
            i += 1
        if i >= n:
            sys.exit("unterminated string")
        tokens.append((True, s[start:i])) # str as tuple with first elem true
        i += 1
        continue
    start = i
    while i < n and not s[i].isspace():
        i += 1
    tokens.append((False, s[start:i]))

content = tokens

i = 0
while i < len(content):
    tok_is_str, tok = content[i]
    if tok == "import":
        i += 2
        continue
    if tok == "word":
        name = content[i + 1][1]
        i += 2
        start = i
        depth = 1
        while i < len(content):
            _, t = content[i]
            if t == "word":
                depth += 1
            elif t == "end":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        if depth != 0:
            sys.exit(f"missing end in word: {name}")
        words[name] = content[start:i + 1]
        i += 1
        continue
    i += 1

def run(code):
    pc = 0
    while pc < len(code):
        is_str, op = code[pc]
        if is_str:
            stack.append(op)
            pc += 1
            continue
        if op.lstrip("-").isdigit():
            stack.append(int(op))
        elif op == "+" or op == "add":
            b = stack.pop()
            a = stack.pop()
            stack.append(a + b)
        elif op == "swap":
            stack[-1], stack[-2] = stack[-2], stack[-1]
        elif op == "over":
            stack.append(stack[-2])
        elif op == "puti":
            print(stack.pop(), end="")
        elif op == "cr":
            print()
        elif op == "puts":
            v = stack.pop()
            print(v)
        elif op == "<":
            b = stack.pop()
            a = stack.pop()
            stack.append(1 if a < b else 0)
        elif op == "while":
            cond_start = pc + 1
            j = cond_start
            while j < len(code) and code[j][1] != "do":
                j += 1
            if j >= len(code):
                sys.exit("missing do")
            cond_end = j
            body_start = j + 1
            depth = 1
            j = body_start
            while j < len(code):
                if code[j][1] == "while":
                    depth += 1
                elif code[j][1] == "end":
                    depth -= 1
                    if depth == 0:
                        break
                j += 1
            if depth != 0:
                sys.exit("missing end in while")
            body_end = j
            while True:
                run(code[cond_start:cond_end])
                if not stack.pop():
                    break
                run(code[body_start:body_end])
            pc = body_end
            continue
        elif op == "end":
            pass
        else:
            if op in words:
                run(words[op])
            else:
                sys.exit(f"unknown op: {op}")
        pc += 1

if "main" not in words:
    sys.exit("no main word defined")

run(words["main"])

