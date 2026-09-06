with open("templates/projects.html", "r", encoding="utf-8") as f:
    lines = f.readlines()

stack = []
for idx, line in enumerate(lines, 1):
    if "{% for " in line:
        stack.append((idx, line.strip()))
    if "{% endfor %}" in line:
        if stack:
            stack.pop()
        else:
            print(f"Orphaned {{% endfor %}} at line {idx}")

if stack:
    print("\n--- UNCLOSED LOOPS FOUND ---")
    for line_num, content in stack:
        print(f"Line {line_num}: {content}")
else:
    print("All loops are properly balanced locally!")