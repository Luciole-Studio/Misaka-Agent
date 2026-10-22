# Models

Every agent needs a model to think with. This page covers signing in to a provider, choosing
which model each agent uses, and running a model on your own machine.

## Signing in

`/login` in chat asks how you want to sign in, then lists the providers for that way.
`/login PROVIDER` goes straight to one. In the terminal, `misaka setup model` does the same, and
`/logout` signs out.

**With an account you already have**, through the browser: ChatGPT Plus or Pro ("Sign in with
ChatGPT"), GitHub Copilot, xAI (SuperGrok or X Premium), OpenRouter, Kimi Code, Meta and a few
others. A Claude account signs in the same way; Anthropic bills that use per token as extra usage,
outside your plan's limits. For a Claude account you choose between *Browser login* and *Copy code
login*: the second is for a machine without a browser, where you open the address on another
device and paste the code it shows back into MISAKA. ChatGPT signs in to the `openai` provider
itself; the older `openai-codex` provider remains for sign-ins made with it.

**With an API key**, billed per use: Anthropic, OpenAI, Google, Mistral, OpenRouter, xAI,
DeepSeek and many more. Amazon Bedrock uses your AWS credentials instead.

**With Radius**, pi's model gateway (`https://radius.pi.dev`): the last choice of `/login`. Its
models are the ones your Radius organization allows.

Sign-ins are stored in `~/.misaka/credentials/auth.json`, readable only by you.

A key in your shell, or in `~/.misaka/.env`, is picked up without signing in (the shell's wins):

| Provider | Variable |
|---|---|
| Anthropic | `ANTHROPIC_API_KEY`; or `ANTHROPIC_OAUTH_TOKEN`, a Claude account's token; or `ANTHROPIC_AUTH_TOKEN`, sent as a bearer token (for a gateway in front of Anthropic) and used first |
| OpenAI | `OPENAI_API_KEY` |
| Google | `GEMINI_API_KEY` |
| Mistral | `MISTRAL_API_KEY` |
| xAI | `XAI_API_KEY` |
| OpenRouter | `OPENROUTER_API_KEY` |
| DeepSeek | `DEEPSEEK_API_KEY` |
| NVIDIA | `NVIDIA_API_KEY` |
| GitHub Copilot | `COPILOT_GITHUB_TOKEN` |
| TypeSafe (classifiers) | `TYPESAFE_API_KEY` |
| Radius | `RADIUS_API_KEY` |
| Amazon Bedrock | your AWS settings, such as `AWS_PROFILE` |

Without any Anthropic key, MISAKA can sign in to Anthropic with workload identity federation:
set `ANTHROPIC_FEDERATION_RULE_ID`, `ANTHROPIC_ORGANIZATION_ID` and `ANTHROPIC_IDENTITY_TOKEN_FILE`
(a file holding your identity provider's token), and if your rule needs them,
`ANTHROPIC_SERVICE_ACCOUNT_ID` and `ANTHROPIC_WORKSPACE_ID`. Anthropic's SDK exchanges that token
for access and refreshes it.

`misaka auth check` shows which providers are ready, without sending a request.

## Choosing who uses which model

There is one default model for the whole team, and each agent can have her own instead.

| To | Do this |
|---|---|
| set the team's default | `misaka setup model`, and choose "Global default" |
| give Last Order her own model | in her window: `/model`, highlight a model, press Ctrl+S. Or `misaka setup model`, and choose her. |
| give a Sister her own model | in her window (`/sister 10032`): `/model`, then Ctrl+S. Or when creating her: `misaka create 10036 --model provider/model` |
| switch model for this conversation only | `/model`, then Enter. Or `/model NAME` |
| cycle through your favourite models | Ctrl+P. `/scoped-models` chooses which models are in the cycle |

A team can mix providers. A common arrangement is a strong model for Last Order, who plans and
writes the conclusions, and cheaper ones for Sisters who do the reading.

A model without vision cannot be shown an image: a scanned page, a map from `doc_page_image`, a
screenshot, a picture you paste. Set one vision model for the whole team, `"vision": {"model":
"provider/model"}` in `settings.json`, and every image a request carries is read to such a model
in words, by that vision model; each image is read once and kept. A model that can see is sent
the image itself and the vision model is not used.

## Classifiers and image models

Some providers also offer models that do not chat. A **classifier** answers typed questions about
data you hand it (yes or no, a score, one of a list), often with a probability, and some also look at
images; TypeSafe offers only these, and OpenAI, OpenRouter, Vercel AI Gateway and Cloudflare offer
some. An **image model** draws pictures from a prompt; OpenRouter offers these. Neither appears in
`/model`. An agent reaches them from a `codemode` script, through its `models` object, with the
same sign-ins as the chat models; the reference shipped with MISAKA (`codemode.md`) shows how.
llama.cpp's decision models are classifiers too.

## Thinking levels

Models that reason before answering take a thinking level: `off`, `minimal`, `low`, `medium`,
`high`, `xhigh` or `max`, as far as the model supports them. Higher levels are slower and cost
more.

| To | Do this |
|---|---|
| change it for this conversation | `/thinking LEVEL`, or Shift+Tab to cycle |
| make it this agent's default | `/thinking --default LEVEL` in her window |
| set it for a new Sister | `misaka create 10036 --thinking high` |

## A model on your own machine

### llama.cpp

MISAKA works with a [llama.cpp](https://github.com/ggml-org/llama.cpp) server directly. Start the
server, then type `/login llama.cpp` in chat and give its address (Enter keeps
`http://127.0.0.1:8080`). You can instead set `LLAMA_BASE_URL`, and `LLAMA_API_KEY` if your
server wants a key. The server's models then appear in `/model` as `llama.cpp/NAME`.

Decision models, such as Julia-1 or OpenJev, answer only classification questions, so they stay
out of `/model`; codemode scripts reach them as classifiers. llama.cpp 0.6.0 and later mark these
models in their model list. An older server does not, and its decision models show up in `/model`
like the others.

`/llama` in chat manages the server's models: load and unload them, or search Hugging Face for a
model and have the server download it (this needs a server started in router mode, and
`HF_TOKEN` for gated models). This needs the `openai` extra, which `providers` includes.

### Ollama, LM Studio, vLLM and others

Any server that speaks the OpenAI API can be added: Ollama, LM Studio, vLLM, or a gateway your
institution runs. Describe it in `~/.misaka/models.json`:

```json
{
  "providers": {
    "ollama": {
      "baseUrl": "http://localhost:11434/v1",
      "api": "openai-completions",
      "apiKey": "ollama",
      "models": [
        { "id": "qwen3:32b", "contextWindow": 32768, "maxTokens": 8192 }
      ]
    }
  }
}
```

| Field | Meaning |
|---|---|
| `baseUrl` | where the server listens |
| `api` | the protocol: `openai-completions` for OpenAI-compatible servers, `anthropic-messages` for Anthropic-compatible ones. It can also be set per model. |
| `apiKey` | the key to send; local servers usually accept any text. `$NAME` reads it from an environment variable, and `!command` from a command's output. |
| `models[].id` | the model's name as the server knows it |
| `contextWindow`, `maxTokens` | the model's limits, in tokens; 128000 and 16384 when left out |
| `models[].reasoning` | `true` for a model that thinks before answering, so it takes a thinking level |
| `models[].samplingParams` | sampling values sent with every request, such as `temperature` or `top_p` |
| `models[].samplingParamsByThinkingLevel` | sampling values for one thinking level (`off`, `minimal`, `low`, `medium`, `high`, `xhigh`, `max`), applied over `samplingParams` |

The model then appears in `/model` as `ollama/qwen3:32b`. An editor can check the file as you
type: see [Schemas for your editor](../reference/configuration.md#schemas-for-your-editor).

Some local thinking models want different sampling with thinking on and off. For example:

```json
{
  "id": "qwen3:32b",
  "reasoning": true,
  "samplingParams": { "temperature": 1.0, "top_p": 0.95 },
  "samplingParamsByThinkingLevel": {
    "off": { "temperature": 0.7, "top_p": 0.8 },
    "high": { "top_k": 20 }
  }
}
```

A level the model does not support is first moved to the next higher level it does support, or
else the next lower one. The values for that level are laid over `samplingParams` key by key, and
a level with no entry keeps `samplingParams` as they are. Only the `openai-completions` and `openai-responses` protocols (and
Azure's Responses API) send these values; other protocols ignore them. Keep the temperature above
zero: at zero, an agent tends to repeat the same step.

Research asks a lot of a model: long contexts, many tool calls, careful reading. Small local
models often struggle with it. A workable arrangement is a hosted model for Last Order and local
ones for some Sisters.

## Several models answering as one

`misaka moa configure` sets up a Mixture-of-Agents preset: several models answer the same prompt
and one combines their answers. The preset appears in `/model` like any other model once the
combining model's provider is signed in. `misaka moa list` shows your presets, and
`misaka moa delete NAME` removes one. A conversation you resume stays on its preset, although
the combining model wrote the replies.

## When a model does not work

| What you see | What to do |
|---|---|
| `No API key found for ...` | Sign in with `/login`, or set the provider's key. |
| `The ... package is not installed` | The provider's SDK is missing. Reinstall with the `providers` extra: see [Getting started](../getting-started.md#2-install-misaka). |
| the wizard says the test request failed | The key is stored, but the provider refused it. Check the key and run `misaka setup model` again. |
| `Invalid models.json ...` | The file has a mistake, and the message names it. A custom provider needs `baseUrl`, `apiKey` and an `api`. |

More in [Troubleshooting](troubleshooting.md).
