# Whiskers

A cat that lives in your Discord server. Whiskers starts out as an ordinary cat: it meows, knocks things over and ignores you when it feels like it. It answers whenever someone mentions it, replies to it or says its name, and it sometimes joins conversations on its own. As people talk to it, Whiskers keeps a journal of who it's becoming. Teach it words, give it a storyline or spoil it with treats, and that's who it grows into.

Everything runs on your own server with a local AI model. There's no API key and no outside service involved.

## Can your server run it?

The AI model runs on the CPU, so RAM and CPU cores are what count.

| | Minimum | Recommended |
| :--- | :--- | :--- |
| RAM | 4 GB | 8 GB |
| CPU | 2 cores | 4 or more cores |
| Disk | 6 GB | 10 GB |
| Python | 3.10 | 3.11 or newer |

- The cheap and free WispByte bot plans (around 512 MB of RAM) are too small for any local model. You need a plan with at least 4 GB.
- An 8 GB server with an AMD EPYC 7351P runs the default model comfortably.
- **For good roleplay, use a 12 to 16 GB server and `MODEL=nemo-12b`.** The small default model is quick but loses the thread of a scene. `nemo-12b` follows roleplay and context much better, but replies take about three times as long. `llama3.1-8b` sits in between.
- **Replies are slow compared with a cloud bot.** On 2 to 4 shared CPU cores, expect about 10 to 30 seconds for a normal reply. The first reply after a restart, or in a channel Whiskers hasn't talked in for a while, can take a minute because the model has to read the whole conversation first. Whiskers shows "typing..." while it thinks. `/whiskers status` shows the real speed on your server.
- A 3 to 4 billion parameter model is a lot less clever than the big models shapes.inc used. It's good at being a cat, but it will sometimes lose the thread. If you later want more brains, see [Using a different AI server](#using-a-different-ai-server).

Ask your host whether their rules allow constant CPU use before you buy. Whiskers only uses the CPU while it's thinking, but some hosts suspend servers that run at full CPU often.

## 1. Create the Discord bot

1. Go to the [Discord Developer Portal](https://discord.com/developers/applications), click **New Application** and name it Whiskers.
2. Open **Bot**, click **Reset Token** and copy the token. You'll need it in step 2.
3. On the same page, turn on **Message Content Intent** and save. Without it Whiskers can't read chat.
4. Invite the bot. Replace `YOUR_CLIENT_ID` with the Application ID from **General Information**:
   ```text
   https://discord.com/oauth2/authorize?client_id=YOUR_CLIENT_ID&permissions=274877975616&scope=bot+applications.commands
   ```
   This asks for View Channels, Send Messages, Send Messages in Threads, Read Message History and Add Reactions.

## 2. Put it on the server

### On WispByte (or another Pterodactyl panel)

1. Create a **Python** server (Python 3.11 or newer if you can choose).
2. Upload the files: download this repository as a ZIP, upload it in the **File Manager** and unarchive it so `main.py` is in the top folder. If your panel has a Git repository setting, you can point it at this repo instead.
3. In **Startup**, set the startup/app file to `main.py` and the requirements file to `requirements.txt`.
4. In the File Manager, make a copy of `.env.example` named `.env`, open it and paste your token after `DISCORD_TOKEN=`.
5. Start the server and watch the console. On the first start Whiskers:
   - downloads the llama.cpp AI engine (about 20 MB),
   - downloads the model (about 2.5 GB, a few minutes),
   - loads the model, and prints `The AI engine is ready`.

   Whiskers comes online in Discord right away and shows **💤 waking up** as its status until the model is loaded. Later starts skip the downloads.

**Updating:** if your panel pulls the code from GitHub, keep `SYNC_WITH_GIT=true` in `.env` and just restart the server to update. On every start Whiskers makes its code match GitHub exactly, so a file you deleted, edited or uploaded by hand can't block the update. Your `.env`, `data/`, `models/` and `engine/` are never touched. Change `personality.txt` on GitHub rather than on the server, or the server copy gets replaced. The console shows `[update] Code matches ...` when it has synced.

If the console says the prebuilt engine doesn't run on your server (this happens on Alpine-based images), Whiskers builds llama.cpp from source by itself. That takes 10 to 30 minutes and only happens once.

### On a normal Linux VPS or PC

```bash
git clone https://github.com/Megildur/whiskers.git
cd whiskers
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # then put your token in .env
python main.py
```

To keep it running after you log out, use your host's process manager, `tmux`, or a systemd service.

## 3. Let Whiskers in

Whiskers only reads and talks in channels you allow. Nothing happens until you do this:

```text
/whiskers channel allow channel:#general
```

You can allow a whole category at once, and threads follow their parent channel. If you'd rather have Whiskers everywhere, use `/whiskers mode` and pick **Everywhere except blocked channels**, then `/whiskers channel block` the places it shouldn't go.

## Commands

| Command | Who | What it does |
| :--- | :--- | :--- |
| `/whiskers channel allow` / `block` / `unset` | Manage Server | Let Whiskers into a channel, thread or category, keep it out, or go back to the default. |
| `/whiskers channel list` | Manage Server | Where Whiskers can talk. |
| `/whiskers mode` | Manage Server | Only allowed channels (default), or everywhere except blocked ones. |
| `/whiskers chattiness <percent> [channel]` | Manage Server | How often Whiskers joins conversations uninvited. Default 5%. 0 means only when called. |
| `/whiskers options` | Manage Server | Emoji reactions, answering to its name, and pinging on replies. |
| `/whiskers status` | Manage Server | Settings, brain status, speed and memory counts. |
| `/whiskers simulate [scene]` | Manage Server | Plays test scenes (catnip, window, dinner, stranger, chaos, belly) through the model with throwaway memory. A public message shows live progress; the transcript, with Whiskers' private notes on what it understood, is posted in the channel when it's done. |
| `/whiskers personality edit` | Manage Server | Rewrite the journal by hand. |
| `/whiskers personality evolve` | Manage Server | Make Whiskers reflect and grow right now. |
| `/whiskers owner [member]` | Manage Server | Who Whiskers belongs to and adores most. Defaults to the server owner. |
| `/whiskers personality speech` | Manage Server | Just a cat (meows and actions only), or able to communicate. |
| `/whiskers personality undo` / `reset` | Manage Server | Go back one journal version, or back to an ordinary cat. |
| `/whiskers memory forget <member>` / `wipe` | Manage Server | Forget one person, or everything in this server. |
| `/cat journal` | Everyone | Read Whiskers' journal. |
| `/cat memories` | Everyone | What Whiskers remembers about you (only you see it). |
| `/cat forget-me` | Everyone | Make Whiskers forget you. |

Server admins can change who sees the `/whiskers` commands in **Server Settings > Integrations > Whiskers**.

## How Whiskers behaves

- **Always answers** when someone @mentions it, replies to one of its messages or says "Whiskers". If someone keeps talking right after Whiskers answered them, it usually answers again.
- **Is devoted to its human** (the server owner unless you pick someone with `/whiskers owner`), and is curious and friendly with everyone else, warming up the more they pet, feed and play with it.
- **Plays along with roleplay.** What people do in *asterisks* really happens to Whiskers: give it food and it eats, throw it out the window and it lands outside.
- **Sometimes chimes in** on its own, based on the chattiness setting. It's more likely to when people talk about cat things like treats, fish or lasers, and it won't take over a channel.
- **Reacts with emoji** now and then: 🐟 for treats, 🙀 for the vacuum, 💤 for bedtime.
- **💤 on your message** means Whiskers is still waking up or too many people are waiting. Try again in a bit.
- It ignores other bots, so two bots can't get stuck talking to each other.

## How Whiskers grows

Whiskers' personality has two layers:

1. **`personality.txt`**: the starting personality, an ordinary cat. Edit it if you want a different starting point.
2. **The journal**: a short list in Whiskers' own words about who it is now. It starts as "I'm Whiskers, a cat. I can't talk." After every 25 messages Whiskers sends (and at most once every 2 hours), while nobody is waiting for a reply, it rereads recent chats and its memories and updates the journal a little. New habits, favourite people, nicknames and running jokes show up there. Unusual abilities like superpowers only appear once the server has built toward them over time.

The journal is the truth for Whiskers: whatever it says, Whiskers does. You can steer it with `/whiskers personality edit`, roll back a change you don't like with `undo`, or start over with `reset`. Each server has its own Whiskers.

### Learning to talk

Whiskers starts as just a cat. Its replies are only *actions* and cat sounds (mrrp, meow, purr, hiss) and emoji. Small models slip into English even when told not to, so any human words are also removed from its replies before they're sent.

It can learn to communicate, the way your old cat started talking through thoughts. That only becomes possible after Whiskers has been in the server for a week (`SPEECH_AFTER_DAYS` in `.env`) and has updated its journal at least 5 times. From then on, each journal update checks whether people have kept teaching or encouraging it to talk. Once they have, Whiskers learns to get simple thoughts across, and the journal says how.

You can also decide yourself with `/whiskers personality speech`: lock it back to just a cat, or let it communicate right away. `reset` makes it an ordinary cat that can't talk again. `/cat journal` and `/whiskers status` show which stage it's at.

## What it remembers

- **Recent chat**: messages in allowed channels, kept for a week. Whiskers reads the last couple dozen messages before it answers.
- **Memories**: while things are quiet, Whiskers turns chat into short diary entries ("Alice gave Whiskers a laser pointer and it chased it for an hour") and recalls the relevant ones when a topic comes up again.
- **People**: facts about each person (pets, hobbies, how they treat Whiskers) and how well it knows them.

It never reads channels it isn't allowed in, deleted and edited messages are updated in its chat log, and people can see or erase what it knows about them with `/cat memories` and `/cat forget-me`. Everything is stored in `data/whiskers.db` on your server.

## Settings in .env

The commented `.env.example` lists them all. The ones you might change:

- `MODEL`: which model Whiskers thinks with.
  - `nemo-12b`: best roleplay and understanding of what's going on. Needs about 10 GB of RAM; about 3x slower than `qwen3-4b`.
  - `llama3.1-8b`: good roleplay, faster. Needs about 7 GB of RAM; about 2x slower than `qwen3-4b`.
  - `qwen3-4b` (default): quick, for 4 to 8 GB servers, but weaker at roleplay.
  - `llama3.2-3b` if replies are too slow, or `qwen2.5-1.5b` on a 2 to 3 GB server.

  After changing it, restart. The new model downloads once; you can delete the old `.gguf` from `models/` to free space.
- `LLM_THREADS`: CPU threads for the model. Leave it empty; Whiskers reads your plan's CPU limit.
- `LLM_CONTEXT`: lower it to `4096` if the server runs out of memory.
- `TIMEZONE`: so Whiskers knows when it's night (for example `America/New_York`).
- `OWNER_IDS`: your Discord user ID, to use the admin commands in any server.

## Testing changes

`/whiskers simulate` plays the same scripted scenes every time, so after changing `personality.txt`, the model or the temperature you can run it again and compare the transcripts. Each line shows Whiskers' private note on the scene, which tells you whether a bad reply came from misunderstanding the situation or from how it acted on it. The scenes live in `whiskers/simulate.py`; add your own there. Real chats wait while a simulation reply is being written, so run it when the server is quiet.

## Using a different AI server

Set `LLM_BACKEND=external` in `.env` to use any OpenAI-compatible server instead of the built-in one. For example, [Ollama](https://ollama.com) on a gaming PC with a GPU (`LLM_BASE_URL=http://your-pc:11434/v1`, `LLM_MODEL_NAME=qwen3:8b`), or a hosted API with `LLM_API_KEY`. Bigger models make a smarter cat, and memories and the journal keep working the same way.

## Troubleshooting

- **Whiskers is online but never answers.** Allow a channel with `/whiskers channel allow`, check that Message Content Intent is on, and check that its role can send messages there. `/whiskers status` shows whether the brain is ready.
- **The server keeps restarting or the AI crashes.** It's probably out of RAM. Use a smaller `MODEL` or set `LLM_CONTEXT=4096`. The model's own log is in `data/llama-server.log`.
- **Replies are too slow.** Use a smaller `MODEL` (`nemo-12b` → `llama3.1-8b` → `qwen3-4b`), lower `HISTORY_MESSAGES`, or get more CPU cores.
- **Whiskers forgets what's going on mid-scene or plays it too safe.** Use a bigger `MODEL`. Small models lose track of a scene quickly.
- **The model download fails.** Restart; the download resumes where it stopped. You can also download the `.gguf` yourself, upload it to `models/` and set `MODEL_PATH`.
- **Slash commands don't show up.** They're published on startup and can take a minute. Restarting Discord helps.

## Project layout

| Path | What it is |
| :--- | :--- |
| `main.py` | Starts the bot. |
| `personality.txt` | Whiskers' starting personality. |
| `whiskers/engine.py` | Downloads, builds if needed and runs llama.cpp. |
| `whiskers/brain.py` | When to talk, what to remember, how to grow. |
| `whiskers/prompts.py` | What the model is told. |
| `whiskers/db.py` | The SQLite memory. |
| `whiskers/cogs/` | Discord events and slash commands. |
| `data/`, `engine/`, `models/` | Created on first start; not in git. |

Run the tests with `pip install pytest && python -m pytest`.
