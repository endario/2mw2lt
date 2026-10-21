// One recorded Claude Code session (2.1.306, headless, through a probe plugin whose name is replaced
// by 2mw2lt's; paths and names replaced by placeholders): a slash command, a skill, thinking, a
// desk prompt that started a turn and one appended into it, a failed call, notices, attachments, a
// queue record and a compaction. `transcript` is its transcript's lines, which history reads
// (machine/internal/surface/claude/history_test.go); `live` is what the module's session.append hook
// was handed for the same rows, and `acts` the desk acts that landed its desk prompts. Both paths
// must make `rows`, the view's rows, in order; a reload then draws what the live view drew.
// The Go test reads this file as JSON after the marker below, so it holds nothing else.
export const SLICE = {
 "transcript": [
  "{\"type\":\"queue-operation\",\"content\":\"/example:hello world\",\"operation\":\"enqueue\"}",
  "{\"type\":\"queue-operation\",\"operation\":\"dequeue\"}",
  "{\"type\":\"attachment\",\"uuid\":\"0afcf020-8971-40a4-b9c2-d16c720825f5\",\"parentUuid\":null,\"isSidechain\":false,\"attachment\":{\"type\":\"hook_success\"}}",
  "{\"type\":\"attachment\",\"uuid\":\"ac544cf5-da6f-4616-afe9-0b626598df85\",\"parentUuid\":\"0afcf020-8971-40a4-b9c2-d16c720825f5\",\"isSidechain\":false,\"attachment\":{\"type\":\"hook_additional_context\"}}",
  "{\"type\":\"user\",\"uuid\":\"fbb8f277-7ac2-4967-9ab2-cd2c9c6adb6c\",\"parentUuid\":\"ac544cf5-da6f-4616-afe9-0b626598df85\",\"isSidechain\":false,\"message\":{\"role\":\"user\",\"content\":\"<command-message>example:hello</command-message>\\n<command-name>/example:hello</command-name>\\n<command-args>world</command-args>\"}}",
  "{\"type\":\"user\",\"uuid\":\"6c617233-ac18-49ae-a969-7c3a2584d285\",\"parentUuid\":\"fbb8f277-7ac2-4967-9ab2-cd2c9c6adb6c\",\"isSidechain\":false,\"isMeta\":true,\"message\":{\"role\":\"user\",\"content\":[{\"type\":\"text\",\"text\":\"Reply with exactly: hello world\\n\"}]}}",
  "{\"type\":\"assistant\",\"uuid\":\"2de6ef72-07ff-4f95-ab21-4d138eb0c5d2\",\"parentUuid\":\"28f85012-2547-4207-9334-640842cd93ad\",\"isSidechain\":false,\"message\":{\"role\":\"assistant\",\"content\":[{\"type\":\"thinking\",\"thinking\":\"\",\"signature\":\"c2ln\"}]}}",
  "{\"type\":\"assistant\",\"uuid\":\"5a01728d-a452-4ac2-8760-3b7c21f615ec\",\"parentUuid\":\"2de6ef72-07ff-4f95-ab21-4d138eb0c5d2\",\"isSidechain\":false,\"message\":{\"role\":\"assistant\",\"content\":[{\"type\":\"tool_use\",\"id\":\"toolu_01UWK8nty58bWt5fSAoY5fqo\",\"name\":\"Skill\",\"input\":{\"skill\":\"example:hello\",\"args\":\"world\"},\"caller\":{\"type\":\"direct\"}}]}}",
  "{\"type\":\"user\",\"uuid\":\"f45a9dd1-7f3b-4ea7-acc3-857f31a72ed7\",\"parentUuid\":\"5a01728d-a452-4ac2-8760-3b7c21f615ec\",\"isSidechain\":false,\"message\":{\"role\":\"user\",\"content\":[{\"type\":\"tool_result\",\"tool_use_id\":\"toolu_01UWK8nty58bWt5fSAoY5fqo\",\"content\":\"Launching skill: example:hello\"}]}}",
  "{\"type\":\"user\",\"uuid\":\"ee0285bb-1d1d-425b-94bf-f8e1f9bd9bb0\",\"parentUuid\":\"f45a9dd1-7f3b-4ea7-acc3-857f31a72ed7\",\"isSidechain\":false,\"isMeta\":true,\"message\":{\"role\":\"user\",\"content\":\"Skill /example:hello is already loaded above; instructions unchanged. Arguments: world\"}}",
  "{\"type\":\"assistant\",\"uuid\":\"bf892c96-6974-4684-8844-8823ab22ce2c\",\"parentUuid\":\"f0e6ac78-f09d-458f-8be6-a077c709e4b5\",\"isSidechain\":false,\"message\":{\"role\":\"assistant\",\"content\":[{\"type\":\"thinking\",\"thinking\":\"\",\"signature\":\"c2ln\"}]}}",
  "{\"type\":\"assistant\",\"uuid\":\"51ff52bf-387e-45fe-a5f0-ee3158b89555\",\"parentUuid\":\"bf892c96-6974-4684-8844-8823ab22ce2c\",\"isSidechain\":false,\"message\":{\"role\":\"assistant\",\"content\":[{\"type\":\"text\",\"text\":\"hello world\"}]}}",
  "{\"type\":\"system\",\"uuid\":\"65aaabcb-5423-4460-914e-0fd9c31ffce7\",\"parentUuid\":\"51ff52bf-387e-45fe-a5f0-ee3158b89555\",\"isSidechain\":false,\"subtype\":\"stop_hook_summary\"}",
  "{\"type\":\"queue-operation\",\"content\":\"from: console user alice\\n\\nRun the shell command `ls /nonexistent-path`, then reply with the word: two\",\"operation\":\"enqueue\"}",
  "{\"type\":\"queue-operation\",\"operation\":\"dequeue\"}",
  "{\"type\":\"user\",\"uuid\":\"4826ba01-9cc5-448c-818b-b1776053dc4d\",\"parentUuid\":\"65aaabcb-5423-4460-914e-0fd9c31ffce7\",\"isSidechain\":false,\"origin\":{\"kind\":\"plugin\",\"name\":\"2mw2lt\",\"asUser\":true},\"message\":{\"role\":\"user\",\"content\":\"from: console user alice\\n\\nRun the shell command `ls /nonexistent-path`, then reply with the word: two\"}}",
  "{\"type\":\"user\",\"uuid\":\"615c318e-5b99-454c-a1cc-feea31aba314\",\"parentUuid\":\"4826ba01-9cc5-448c-818b-b1776053dc4d\",\"isSidechain\":false,\"isMeta\":true,\"origin\":{\"kind\":\"plugin\",\"name\":\"2mw2lt\"},\"message\":{\"role\":\"user\",\"content\":[{\"type\":\"text\",\"text\":\"from: console user alice\\n\\nAlso, after that, reply with the word: three\"}]}}",
  "{\"type\":\"assistant\",\"uuid\":\"95b05df9-554f-412e-9edd-5172e439fe14\",\"parentUuid\":\"615c318e-5b99-454c-a1cc-feea31aba314\",\"isSidechain\":false,\"message\":{\"role\":\"assistant\",\"content\":[{\"type\":\"thinking\",\"thinking\":\"\",\"signature\":\"c2ln\"}]}}",
  "{\"type\":\"assistant\",\"uuid\":\"525094f6-e1a3-4465-a5a8-dc37d8583b80\",\"parentUuid\":\"95b05df9-554f-412e-9edd-5172e439fe14\",\"isSidechain\":false,\"message\":{\"role\":\"assistant\",\"content\":[{\"type\":\"tool_use\",\"id\":\"toolu_01FrvZv2kSJpknvUFWRGVffy\",\"name\":\"Bash\",\"input\":{\"command\":\"ls /nonexistent-path\",\"description\":\"List a nonexistent path to check its output\"},\"caller\":{\"type\":\"direct\"}}]}}",
  "{\"type\":\"user\",\"uuid\":\"d083504c-3ced-4d59-905a-786efdb6705d\",\"parentUuid\":\"525094f6-e1a3-4465-a5a8-dc37d8583b80\",\"isSidechain\":false,\"message\":{\"role\":\"user\",\"content\":[{\"type\":\"tool_result\",\"content\":\"Exit code 2\\n\\\"/nonexistent-path\\\": No such file or directory (os error 2)\",\"is_error\":true,\"tool_use_id\":\"toolu_01FrvZv2kSJpknvUFWRGVffy\"}]}}",
  "{\"type\":\"assistant\",\"uuid\":\"f1326891-586a-4281-bae2-5fd49717c32f\",\"parentUuid\":\"484272db-6b51-4ec4-8ea3-81f564330433\",\"isSidechain\":false,\"message\":{\"role\":\"assistant\",\"content\":[{\"type\":\"thinking\",\"thinking\":\"\",\"signature\":\"c2ln\"}]}}",
  "{\"type\":\"assistant\",\"uuid\":\"9e4ab9be-5003-477a-925b-a74474e3d815\",\"parentUuid\":\"f1326891-586a-4281-bae2-5fd49717c32f\",\"isSidechain\":false,\"message\":{\"role\":\"assistant\",\"content\":[{\"type\":\"text\",\"text\":\"The command exited with code 2: `/nonexistent-path: No such file or directory`.\\n\\ntwo\\nthree\"}]}}",
  "{\"type\":\"system\",\"uuid\":\"e8a78621-59dc-4cee-9ab7-c78feff559a7\",\"parentUuid\":\"9e4ab9be-5003-477a-925b-a74474e3d815\",\"isSidechain\":false,\"subtype\":\"stop_hook_summary\"}",
  "{\"type\":\"queue-operation\",\"content\":\"/compact\",\"operation\":\"enqueue\"}",
  "{\"type\":\"queue-operation\",\"operation\":\"dequeue\"}",
  "{\"type\":\"system\",\"uuid\":\"9cf73d8c-e918-4328-b90a-13bef5ffcc16\",\"parentUuid\":null,\"isSidechain\":false,\"isMeta\":false,\"subtype\":\"compact_boundary\",\"content\":\"Conversation compacted\"}",
  "{\"type\":\"user\",\"uuid\":\"a6337e5c-5a19-40d4-ad4b-3532c649de35\",\"parentUuid\":\"9cf73d8c-e918-4328-b90a-13bef5ffcc16\",\"isSidechain\":false,\"isCompactSummary\":true,\"message\":{\"role\":\"user\",\"content\":\"This session is being continued from a previous conversation that ran out of context. The summary below covers the earlier portion of the conversation.\\n\\n<tools-used-before-summary>Bash, Skill</tools-used-before-summary>\\nSummary:\\n1. Primary Request and Intent:\\n   - Turn 1: Reply with exactly \\\"hello world\\\" for the `/example:hello world` command. Completed.\\n   - Turn 2: Run `ls /nonexistent-path`, then reply \\\"two\\\", then reply \\\"three\\\". The command has run (it failed as expected). The \\\"two\\\" and \\\"three\\\" replies are still outstanding.\\n\\n2. Key Technical Concepts:\\n   - Skill invocation (example:hello) via the Skill tool.\\n   - Bash tool; a nonexistent path yields exit code 2.\\n\\n3. Files and Code Sections:\\n   - None read, created, or modified. Working directory: /work/example (not a git repo).\\n\\n4. Errors and fixes:\\n   - `ls /nonexistent-path` returned exit code 2: `\\\"/nonexistent-path\\\": No such file or directory (os error 2)`. This was the expected result of the requested command, so no fix was needed.\\n\\n5. Problem Solving:\\n   - No open problems. The example command ran and its failure output was captured.\\n\\n6. All user messages:\\n   - \\\"Reply with exactly: hello world\\\" (with `/example:hello world`).\\n   - \\\"from: console user alice — Run the shell command `ls /nonexistent-path`, then reply with the word: two\\\" followed by \\\"Also, after that, reply with the word: three\\\".\\n\\n7. Pending Tasks:\\n   - Reply with the word \\\"two\\\".\\n   - Reply with the word \\\"three\\\" after that.\\n\\n8. Current Work:\\n   - Ran `ls /nonexistent-path` (Bash, exit 2, No such file or directory). The reply \\\"two\\\" and then \\\"three\\\" are next.\\n\\n9. Optional Next Step:\\n   - Send the plain-text reply \\\"two\\\", then \\\"three\\\", as the user asked in the turn-2 message: \\\"reply with the word: two\\\" and \\\"reply with the word: three\\\". No tool calls are needed.\\n\\nIf you need specific details from before compaction (like exact code snippets, error messages, or content you generated), read the full transcript at: /work/example/.claude-home/projects/-work-example/cf07f91c-6016-4316-89bc-47df0545ac75.jsonl\\nContinue the conversation from where it left off without asking the user any further questions. Resume directly — do not acknowledge the summary, do not recap what was happening, do not preface with \\\"I'll continue\\\" or similar. Pick up the last task as if the break never happened.\\n\\nThe messages after this summary are the most recent messages from before compaction, kept verbatim. The summary was written without seeing them, so something it says has not happened yet may already have happened in them.\"}}",
  "{\"type\":\"user\",\"uuid\":\"11f76647-f39f-46a5-89fe-d36ac22dcbed\",\"parentUuid\":\"a6337e5c-5a19-40d4-ad4b-3532c649de35\",\"isSidechain\":false,\"isMeta\":true,\"message\":{\"role\":\"user\",\"content\":\"<local-command-caveat>The command below was run directly in Claude Code, not sent to you as a request, and its output goes straight to the user. It's recorded here as context for later messages.</local-command-caveat>\"}}",
  "{\"type\":\"user\",\"uuid\":\"ccbeabfc-dbc2-4ebf-8b0a-b667c1a44bd1\",\"parentUuid\":\"11f76647-f39f-46a5-89fe-d36ac22dcbed\",\"isSidechain\":false,\"message\":{\"role\":\"user\",\"content\":\"<command-name>/compact</command-name>\\n            <command-message>compact</command-message>\\n            <command-args></command-args>\"}}",
  "{\"type\":\"user\",\"uuid\":\"fbb66b4a-0e7b-4b15-8cab-b7e55821b03a\",\"parentUuid\":\"ccbeabfc-dbc2-4ebf-8b0a-b667c1a44bd1\",\"isSidechain\":false,\"message\":{\"role\":\"user\",\"content\":\"<local-command-stdout>Compacted (ctrl+o to see full summary)</local-command-stdout>\"}}"
 ],
 "live": [
  {
   "door": "command",
   "origin": {
    "kind": "unclassified"
   },
   "uuid": "fbb8f277-7ac2-4967-9ab2-cd2c9c6adb6c",
   "message": {
    "type": "user",
    "role": "user",
    "content": [
     {
      "type": "text",
      "text": "<command-message>example:hello</command-message>\n<command-name>/example:hello</command-name>\n<command-args>world</command-args>"
     }
    ]
   }
  },
  {
   "door": "queue",
   "origin": {
    "kind": "engine"
   },
   "uuid": "af6d3d15-ccac-4b19-a577-4f0b7e5b5131",
   "message": {
    "type": "user",
    "role": "user",
    "content": [
     {
      "type": "text",
      "text": "from: console user alice\n\nRun the shell command `ls /nonexistent-path`, then reply with the word: two"
     }
    ]
   }
  },
  {
   "door": "attachment",
   "origin": {
    "kind": "engine"
   },
   "uuid": "10ccd432-f175-47a0-8847-9d064bd8a5db",
   "message": {
    "type": "attachment",
    "name": "environment",
    "role": "user",
    "content": [
     {
      "type": "text",
      "text": "<system-reminder>\n# Environment\nYou have been invoked in the following environme"
     }
    ]
   }
  },
  {
   "door": "note",
   "origin": {
    "kind": "engine"
   },
   "uuid": "6c617233-ac18-49ae-a969-7c3a2584d285",
   "message": {
    "type": "user",
    "role": "user",
    "isMeta": true,
    "content": [
     {
      "type": "text",
      "text": "Reply with exactly: hello world\n"
     }
    ]
   }
  },
  {
   "door": "response",
   "origin": {
    "kind": "model",
    "model": "claude-haiku-5-5"
   },
   "uuid": "2de6ef72-07ff-4f95-ab21-4d138eb0c5d2",
   "message": {
    "type": "assistant",
    "role": "assistant",
    "content": [
     {
      "type": "thinking",
      "thinking": "",
      "signature": "c2ln"
     }
    ]
   }
  },
  {
   "door": "response",
   "origin": {
    "kind": "model",
    "model": "claude-haiku-5-5"
   },
   "uuid": "5a01728d-a452-4ac2-8760-3b7c21f615ec",
   "message": {
    "type": "assistant",
    "role": "assistant",
    "content": [
     {
      "type": "tool_use",
      "id": "toolu_01UWK8nty58bWt5fSAoY5fqo",
      "name": "Skill",
      "input": {
       "skill": "example:hello",
       "args": "world"
      },
      "caller": {
       "type": "direct"
      }
     }
    ]
   }
  },
  {
   "door": "tool-result",
   "origin": {
    "kind": "tool",
    "tool": "Skill"
   },
   "uuid": "f45a9dd1-7f3b-4ea7-acc3-857f31a72ed7",
   "message": {
    "type": "user",
    "role": "user",
    "content": [
     {
      "type": "tool_result",
      "tool_use_id": "toolu_01UWK8nty58bWt5fSAoY5fqo",
      "content": "Launching skill: example:hello"
     }
    ]
   }
  },
  {
   "door": "tool-message",
   "origin": {
    "kind": "tool",
    "tool": "Skill"
   },
   "uuid": "ee0285bb-1d1d-425b-94bf-f8e1f9bd9bb0",
   "message": {
    "type": "user",
    "role": "user",
    "isMeta": true,
    "content": [
     {
      "type": "text",
      "text": "Skill /example:hello is already loaded above; instructions unchanged. Arguments: world"
     }
    ]
   }
  },
  {
   "door": "response",
   "origin": {
    "kind": "model",
    "model": "claude-haiku-5-5"
   },
   "uuid": "bf892c96-6974-4684-8844-8823ab22ce2c",
   "message": {
    "type": "assistant",
    "role": "assistant",
    "content": [
     {
      "type": "thinking",
      "thinking": "",
      "signature": "c2ln"
     }
    ]
   }
  },
  {
   "door": "response",
   "origin": {
    "kind": "model",
    "model": "claude-haiku-5-5"
   },
   "uuid": "51ff52bf-387e-45fe-a5f0-ee3158b89555",
   "message": {
    "type": "assistant",
    "role": "assistant",
    "content": [
     {
      "type": "text",
      "text": "hello world"
     }
    ]
   }
  },
  {
   "door": "notice",
   "origin": {
    "kind": "engine"
   },
   "uuid": "65aaabcb-5423-4460-914e-0fd9c31ffce7",
   "message": {
    "type": "system",
    "name": "stop_hook_summary",
    "content": []
   }
  },
  {
   "door": "prompt",
   "origin": {
    "kind": "plugin",
    "name": "2mw2lt",
    "asUser": true
   },
   "uuid": "4826ba01-9cc5-448c-818b-b1776053dc4d",
   "message": {
    "type": "user",
    "role": "user",
    "content": [
     {
      "type": "text",
      "text": "from: console user alice\n\nRun the shell command `ls /nonexistent-path`, then reply with the word: two"
     }
    ]
   }
  },
  {
   "door": "note",
   "origin": {
    "kind": "plugin",
    "name": "2mw2lt"
   },
   "uuid": "615c318e-5b99-454c-a1cc-feea31aba314",
   "message": {
    "type": "user",
    "role": "user",
    "isMeta": true,
    "content": [
     {
      "type": "text",
      "text": "from: console user alice\n\nAlso, after that, reply with the word: three"
     }
    ]
   }
  },
  {
   "door": "response",
   "origin": {
    "kind": "model",
    "model": "claude-haiku-5-5"
   },
   "uuid": "95b05df9-554f-412e-9edd-5172e439fe14",
   "message": {
    "type": "assistant",
    "role": "assistant",
    "content": [
     {
      "type": "thinking",
      "thinking": "",
      "signature": "c2ln"
     }
    ]
   }
  },
  {
   "door": "response",
   "origin": {
    "kind": "model",
    "model": "claude-haiku-5-5"
   },
   "uuid": "525094f6-e1a3-4465-a5a8-dc37d8583b80",
   "message": {
    "type": "assistant",
    "role": "assistant",
    "content": [
     {
      "type": "tool_use",
      "id": "toolu_01FrvZv2kSJpknvUFWRGVffy",
      "name": "Bash",
      "input": {
       "command": "ls /nonexistent-path",
       "description": "List a nonexistent path to check its output"
      },
      "caller": {
       "type": "direct"
      }
     }
    ]
   }
  },
  {
   "door": "tool-result",
   "origin": {
    "kind": "tool",
    "tool": "Bash"
   },
   "uuid": "d083504c-3ced-4d59-905a-786efdb6705d",
   "message": {
    "type": "user",
    "role": "user",
    "content": [
     {
      "type": "tool_result",
      "content": "Exit code 2\n\"/nonexistent-path\": No such file or directory (os error 2)",
      "is_error": true,
      "tool_use_id": "toolu_01FrvZv2kSJpknvUFWRGVffy"
     }
    ]
   }
  },
  {
   "door": "response",
   "origin": {
    "kind": "model",
    "model": "claude-haiku-5-5"
   },
   "uuid": "f1326891-586a-4281-bae2-5fd49717c32f",
   "message": {
    "type": "assistant",
    "role": "assistant",
    "content": [
     {
      "type": "thinking",
      "thinking": "",
      "signature": "c2ln"
     }
    ]
   }
  },
  {
   "door": "response",
   "origin": {
    "kind": "model",
    "model": "claude-haiku-5-5"
   },
   "uuid": "9e4ab9be-5003-477a-925b-a74474e3d815",
   "message": {
    "type": "assistant",
    "role": "assistant",
    "content": [
     {
      "type": "text",
      "text": "The command exited with code 2: `/nonexistent-path: No such file or directory`.\n\ntwo\nthree"
     }
    ]
   }
  },
  {
   "door": "notice",
   "origin": {
    "kind": "engine"
   },
   "uuid": "e8a78621-59dc-4cee-9ab7-c78feff559a7",
   "message": {
    "type": "system",
    "name": "stop_hook_summary",
    "content": []
   }
  },
  {
   "door": "compaction",
   "origin": {
    "kind": "engine"
   },
   "uuid": "9cf73d8c-e918-4328-b90a-13bef5ffcc16",
   "message": {
    "type": "system",
    "name": "compact_boundary",
    "content": [
     {
      "type": "text",
      "text": "Conversation compacted"
     }
    ]
   }
  },
  {
   "door": "compaction",
   "origin": {
    "kind": "engine"
   },
   "uuid": "a6337e5c-5a19-40d4-ad4b-3532c649de35",
   "message": {
    "type": "user",
    "role": "user",
    "content": [
     {
      "type": "text",
      "text": "This session is being continued from a previous conversation that ran out of context. The summary below covers the earlier portion of the conversation.\n\n<tools-used-before-summary>Bash, Skill</tools-used-before-summary>\nSummary:\n1. Primary Request and Intent:\n   - Turn 1: Reply with exactly \"hello world\" for the `/example:hello world` command. Completed.\n   - Turn 2: Run `ls /nonexistent-path`, then reply \"two\", then reply \"three\". The command has run (it failed as expected). The \"two\" and \"three\" replies are still outstanding.\n\n2. Key Technical Concepts:\n   - Skill invocation (example:hello) via the Skill tool.\n   - Bash tool; a nonexistent path yields exit code 2.\n\n3. Files and Code Sections:\n   - None read, created, or modified. Working directory: /work/example (not a git repo).\n\n4. Errors and fixes:\n   - `ls /nonexistent-path` returned exit code 2: `\"/nonexistent-path\": No such file or directory (os error 2)`. This was the expected result of the requested command, so no fix was needed.\n\n5. Problem Solving:\n   - No open problems. The example command ran and its failure output was captured.\n\n6. All user messages:\n   - \"Reply with exactly: hello world\" (with `/example:hello world`).\n   - \"from: console user alice — Run the shell command `ls /nonexistent-path`, then reply with the word: two\" followed by \"Also, after that, reply with the word: three\".\n\n7. Pending Tasks:\n   - Reply with the word \"two\".\n   - Reply with the word \"three\" after that.\n\n8. Current Work:\n   - Ran `ls /nonexistent-path` (Bash, exit 2, No such file or directory). The reply \"two\" and then \"three\" are next.\n\n9. Optional Next Step:\n   - Send the plain-text reply \"two\", then \"three\", as the user asked in the turn-2 message: \"reply with the word: two\" and \"reply with the word: three\". No tool calls are needed.\n\nIf you need specific details from before compaction (like exact code snippets, error messages, or content you generated), read the full transcript at: /work/example/.claude-home/projects/-work-example/cf07f91c-6016-4316-89bc-47df0545ac75.jsonl\nContinue the conversation from where it left off without asking the user any further questions. Resume directly — do not acknowledge the summary, do not recap what was happening, do not preface with \"I'll continue\" or similar. Pick up the last task as if the break never happened.\n\nThe messages after this summary are the most recent messages from before compaction, kept verbatim. The summary was written without seeing them, so something it says has not happened yet may already have happened in them."
     }
    ]
   }
  },
  {
   "door": "command",
   "origin": {
    "kind": "unclassified"
   },
   "uuid": "11f76647-f39f-46a5-89fe-d36ac22dcbed",
   "message": {
    "type": "user",
    "role": "user",
    "isMeta": true,
    "content": [
     {
      "type": "text",
      "text": "<local-command-caveat>The command below was run directly in Claude Code, not sent to you as a request, and its output goes straight to the user. It's recorded here as context for later messages.</local-command-caveat>"
     }
    ]
   }
  },
  {
   "door": "command",
   "origin": {
    "kind": "unclassified"
   },
   "uuid": "ccbeabfc-dbc2-4ebf-8b0a-b667c1a44bd1",
   "message": {
    "type": "user",
    "role": "user",
    "content": [
     {
      "type": "text",
      "text": "<command-name>/compact</command-name>\n            <command-message>compact</command-message>\n            <command-args></command-args>"
     }
    ]
   }
  },
  {
   "door": "command",
   "origin": {
    "kind": "unclassified"
   },
   "uuid": "fbb66b4a-0e7b-4b15-8cab-b7e55821b03a",
   "message": {
    "type": "user",
    "role": "user",
    "content": [
     {
      "type": "text",
      "text": "<local-command-stdout>Compacted (ctrl+o to see full summary)</local-command-stdout>"
     }
    ]
   }
  }
 ],
 "acts": {
  "4826ba01-9cc5-448c-818b-b1776053dc4d": "act-1",
  "615c318e-5b99-454c-a1cc-feea31aba314": "act-2"
 },
 "rows": [
  {
   "kind": "row",
   "row": {
    "id": "fbb8f277-7ac2-4967-9ab2-cd2c9c6adb6c",
    "role": "user",
    "origin": "person",
    "blocks": [
     {
      "type": "text",
      "text": "/example:hello world"
     }
    ]
   }
  },
  {
   "kind": "row",
   "row": {
    "id": "6c617233-ac18-49ae-a969-7c3a2584d285",
    "role": "user",
    "origin": "engine",
    "blocks": [
     {
      "type": "text",
      "text": "Reply with exactly: hello world\n"
     }
    ]
   }
  },
  {
   "kind": "row",
   "row": {
    "id": "5a01728d-a452-4ac2-8760-3b7c21f615ec",
    "role": "assistant",
    "origin": "model",
    "blocks": [
     {
      "type": "tool-use",
      "id": "toolu_01UWK8nty58bWt5fSAoY5fqo",
      "tool": "Skill",
      "input": {
       "skill": "example:hello",
       "args": "world"
      }
     }
    ]
   }
  },
  {
   "kind": "row",
   "row": {
    "id": "f45a9dd1-7f3b-4ea7-acc3-857f31a72ed7",
    "role": "tool-result",
    "origin": "engine",
    "blocks": [
     {
      "type": "tool-result",
      "id": "toolu_01UWK8nty58bWt5fSAoY5fqo",
      "text": "Launching skill: example:hello"
     }
    ]
   }
  },
  {
   "kind": "row",
   "row": {
    "id": "ee0285bb-1d1d-425b-94bf-f8e1f9bd9bb0",
    "role": "user",
    "origin": "engine",
    "blocks": [
     {
      "type": "text",
      "text": "Skill /example:hello is already loaded above; instructions unchanged. Arguments: world"
     }
    ]
   }
  },
  {
   "kind": "row",
   "row": {
    "id": "51ff52bf-387e-45fe-a5f0-ee3158b89555",
    "role": "assistant",
    "origin": "model",
    "blocks": [
     {
      "type": "text",
      "text": "hello world"
     }
    ]
   }
  },
  {
   "kind": "row",
   "row": {
    "id": "4826ba01-9cc5-448c-818b-b1776053dc4d",
    "role": "user",
    "origin": "console user alice",
    "blocks": [
     {
      "type": "text",
      "text": "Run the shell command `ls /nonexistent-path`, then reply with the word: two"
     }
    ]
   }
  },
  {
   "kind": "row",
   "row": {
    "id": "615c318e-5b99-454c-a1cc-feea31aba314",
    "role": "user",
    "origin": "console user alice",
    "blocks": [
     {
      "type": "text",
      "text": "Also, after that, reply with the word: three"
     }
    ]
   }
  },
  {
   "kind": "row",
   "row": {
    "id": "525094f6-e1a3-4465-a5a8-dc37d8583b80",
    "role": "assistant",
    "origin": "model",
    "blocks": [
     {
      "type": "tool-use",
      "id": "toolu_01FrvZv2kSJpknvUFWRGVffy",
      "tool": "Bash",
      "input": {
       "command": "ls /nonexistent-path",
       "description": "List a nonexistent path to check its output"
      }
     }
    ]
   }
  },
  {
   "kind": "row",
   "row": {
    "id": "d083504c-3ced-4d59-905a-786efdb6705d",
    "role": "tool-result",
    "origin": "engine",
    "blocks": [
     {
      "type": "tool-result",
      "id": "toolu_01FrvZv2kSJpknvUFWRGVffy",
      "text": "Exit code 2\n\"/nonexistent-path\": No such file or directory (os error 2)",
      "error": true
     }
    ]
   }
  },
  {
   "kind": "row",
   "row": {
    "id": "9e4ab9be-5003-477a-925b-a74474e3d815",
    "role": "assistant",
    "origin": "model",
    "blocks": [
     {
      "type": "text",
      "text": "The command exited with code 2: `/nonexistent-path: No such file or directory`.\n\ntwo\nthree"
     }
    ]
   }
  },
  {
   "kind": "compaction",
   "row": {
    "id": "a6337e5c-5a19-40d4-ad4b-3532c649de35",
    "role": "system",
    "origin": "engine",
    "blocks": [
     {
      "type": "text",
      "text": "This session is being continued from a previous conversation that ran out of context. The summary below covers the earlier portion of the conversation.\n\n<tools-used-before-summary>Bash, Skill</tools-used-before-summary>\nSummary:\n1. Primary Request and Intent:\n   - Turn 1: Reply with exactly \"hello world\" for the `/example:hello world` command. Completed.\n   - Turn 2: Run `ls /nonexistent-path`, then reply \"two\", then reply \"three\". The command has run (it failed as expected). The \"two\" and \"three\" replies are still outstanding.\n\n2. Key Technical Concepts:\n   - Skill invocation (example:hello) via the Skill tool.\n   - Bash tool; a nonexistent path yields exit code 2.\n\n3. Files and Code Sections:\n   - None read, created, or modified. Working directory: /work/example (not a git repo).\n\n4. Errors and fixes:\n   - `ls /nonexistent-path` returned exit code 2: `\"/nonexistent-path\": No such file or directory (os error 2)`. This was the expected result of the requested command, so no fix was needed.\n\n5. Problem Solving:\n   - No open problems. The example command ran and its failure output was captured.\n\n6. All user messages:\n   - \"Reply with exactly: hello world\" (with `/example:hello world`).\n   - \"from: console user alice — Run the shell command `ls /nonexistent-path`, then reply with the word: two\" followed by \"Also, after that, reply with the word: three\".\n\n7. Pending Tasks:\n   - Reply with the word \"two\".\n   - Reply with the word \"three\" after that.\n\n8. Current Work:\n   - Ran `ls /nonexistent-path` (Bash, exit 2, No such file or directory). The reply \"two\" and then \"three\" are next.\n\n9. Optional Next Step:\n   - Send the plain-text reply \"two\", then \"three\", as the user asked in the turn-2 message: \"reply with the word: two\" and \"reply with the word: three\". No tool calls are needed.\n\nIf you need specific details from before compaction (like exact code snippets, error messages, or content you generated), read the full transcript at: /work/example/.claude-home/projects/-work-example/cf07f91c-6016-4316-89bc-47df0545ac75.jsonl\nContinue the conversation from where it left off without asking the user any further questions. Resume directly — do not acknowledge the summary, do not recap what was happening, do not preface with \"I'll continue\" or similar. Pick up the last task as if the break never happened.\n\nThe messages after this summary are the most recent messages from before compaction, kept verbatim. The summary was written without seeing them, so something it says has not happened yet may already have happened in them."
     }
    ]
   }
  },
  {
   "kind": "row",
   "row": {
    "id": "11f76647-f39f-46a5-89fe-d36ac22dcbed",
    "role": "user",
    "origin": "engine",
    "blocks": [
     {
      "type": "text",
      "text": "<local-command-caveat>The command below was run directly in Claude Code, not sent to you as a request, and its output goes straight to the user. It's recorded here as context for later messages.</local-command-caveat>"
     }
    ]
   }
  },
  {
   "kind": "row",
   "row": {
    "id": "ccbeabfc-dbc2-4ebf-8b0a-b667c1a44bd1",
    "role": "user",
    "origin": "person",
    "blocks": [
     {
      "type": "text",
      "text": "/compact"
     }
    ]
   }
  },
  {
   "kind": "row",
   "row": {
    "id": "fbb66b4a-0e7b-4b15-8cab-b7e55821b03a",
    "role": "user",
    "origin": "engine",
    "blocks": [
     {
      "type": "text",
      "text": "Compacted (ctrl+o to see full summary)"
     }
    ]
   }
  }
 ]
}
