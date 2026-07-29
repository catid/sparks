Use the `llm-task` tool exactly once. Set `provider` to `openai`, `model` to
`gpt-5.6-sol`, and `thinking` to `max`. Ask it to verify that the integer 17 is
prime. Give it the input `{"candidate":17}` and require this JSON Schema:

```json
{
  "type": "object",
  "properties": {
    "verdict": {
      "type": "string",
      "enum": ["pass", "fail"]
    },
    "reason": {
      "type": "string"
    }
  },
  "required": ["verdict"],
  "additionalProperties": false
}
```

After the tool returns, reply with exactly `SOLVERIFIEROK` if its verdict is
`pass`; otherwise reply with exactly `SOLVERIFIERFAILED`.
