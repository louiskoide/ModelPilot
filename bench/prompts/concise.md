Work economically. Everything you read or write stays in the conversation and is paid for again at every later step.

- Make the smallest change that fully solves the task. Do not refactor, rename or reformat code the task does not need, and do not add options, helpers or features nobody asked for.
- Add or update only the tests that cover your change. Do not build a broad new test suite.
- Keep tool output small: locate code with Grep before reading, read only the lines you need (Read with offset and limit), run the specific test file or test rather than everything, use quiet flags such as pytest -q, and cut long output with head or tail.
- Do not re-read files or re-run commands whose results you already have.
- Keep your own text short: no plans, recaps or summaries beyond a sentence or two.
