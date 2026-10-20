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
- **Replies are slow compared with a cloud bot.** On 2 to 4 shared CPU cores, expect about 10 to 30 seconds for a normal reply. Whiskers shows "typing..." while it thinks and posts its message the moment it's written. It reads its own instructions ahead of time while nobody is talking, so even the first reply after a restart is quick once it's had a minute to settle in. `/whiskers status` shows the real speed on your server. See [How replies stay fast](#how-replies-stay-fast).
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

Whiskers only reads and talks where you let it. Nothing happens until you do one of these:

```text
/whiskers channel allow channel:#cat-room
/whiskers roam roaming:On
```

- **Home channels** (`/whiskers channel allow`): Whiskers answers **every message** there, whether it's meant for it or not. When several people play with him at once, each person gets their own reply (a Discord reply to their message), so nobody's interaction is lost in someone else's and he keeps straight who did what. Messages someone sends while their reply waits are answered together in it. The one thing he leaves alone is people talking to each other while others are around: a reply to someone else's message, or plain chat like "eh maybe" that isn't roleplay (no *actions*, and not "Milo picks him up"), doesn't name him and doesn't come straight after his own message. That gets an emoji reaction at most. You can allow a whole category at once, and threads follow their parent channel.
- **Roaming** (`/whiskers roam`): Whiskers can also wander into every other channel it can see. There it answers when someone mentions it, replies to it or says its name, and joins in or reacts on its own when something catches its interest (`/whiskers chattiness` sets how often). `/whiskers channel block` keeps it out of a channel completely.

## Commands

| Command | Who | What it does |
| :--- | :--- | :--- |
| `/whiskers channel allow` | Manage Server | Make a channel, thread or category one of Whiskers' homes: it answers every message there. |
| `/whiskers channel block` / `unset` | Manage Server | Keep Whiskers out of a channel completely, or remove its rule. |
| `/whiskers channel list` | Manage Server | Where Whiskers lives and roams. |
| `/whiskers roam` | Manage Server | On: Whiskers also roams every channel it can see, answering when called and joining in when it wants. Off (default): only its home channels. |
| `/whiskers chattiness <percent> [channel]` | Manage Server | While roaming, how often Whiskers joins conversations uninvited. Default 5%. 0 means only when called. |
| `/whiskers mischief <level>` | Manage Server | How often Whiskers starts games and shenanigans on his own: off, sometimes (default) or often. |
| `/whiskers options` | Manage Server | Emoji reactions, answering to its name, and pinging on replies. |
| `/whiskers status` | Manage Server | Settings, brain status, speed and memory counts. |
| `/whiskers simulate [pack] [scene]` | Manage Server | Plays test scenes through the model with throwaway memory. Packs: core, home, food, play, mischief, scary, people, character, magic, long-day, feelings, eleven everyday-Discord packs (discord, discord-hello, -to-him, -chatter, -media, -gaming, -chaos, -server, -people, -odd, -busy), marathon (8 long chats, 326 replies, about 2½ hours), all-discord (210 scenes, about 4 hours) or all (271 scenes, about 8 hours). A public message shows live progress; the transcript, with Whiskers' private notes and automatic warnings, is posted in the channel when it's done. |
| `/whiskers personality edit` | Manage Server | Rewrite the journal by hand. |
| `/whiskers personality evolve` | Manage Server | Make Whiskers reflect and grow right now. |
| `/whiskers owner [member]` | Manage Server | Who Whiskers belongs to and adores most. Defaults to the server owner. |
| `/whiskers personality speech` | Manage Server | Just a cat (meows and actions only), or able to communicate. |
| `/whiskers personality undo` / `reset` | Manage Server | Go back one journal version, or back to an ordinary cat. |
| `/whiskers memory forget <member>` / `wipe` | Manage Server | Forget one person, or everything in this server. |
| `/cat journal` | Everyone | Read Whiskers' journal. |
| `/cat memories` | Everyone | What Whiskers remembers about you, how it feels about you and what you've done that it loved or hated (only you see it). |
| `/cat forget-me` | Everyone | Make Whiskers forget you. |

Server admins can change who sees the `/whiskers` commands in **Server Settings > Integrations > Whiskers**.

## How Whiskers behaves

- **Answers every message in its home channels**, and anywhere when someone @mentions it, replies to one of its messages or says "Whiskers". If someone who just called Whiskers says one more thing right after its answer, it may answer that too, once; to keep talking to it, mention it, reply to it or say its name.
- **Is devoted to its human** (the server owner unless you pick someone with `/whiskers owner`), and is curious and friendly with everyone else, warming up the more they pet, feed and play with it.
- **Plays along with roleplay.** What people do in *asterisks* really happens to Whiskers: give it food and it eats, throw it out the window and it lands outside. It moves like a real cat, one step at a time: to get at someone's breakfast it jumps onto a chair or puts its paws on the table edge first, and it hops off a lap before running off. A toy it loves keeps it busy, through other people's chat and quiet spells, until someone plays with it again.
- **Starts trouble on his own.** He doesn't just wait for people: now and then, in the middle of a chat, he starts something himself (steals a sock, ambushes someone's feet, drops a toy in a lap to start a game). And when one of his home channels goes quiet while people are still around (nobody has said anything for 5 minutes, but someone talked in the last 45), he may start some mischief unprompted, at most once every 3 hours per channel. Each time he gets one idea picked at random from a couple of dozen (a sock heist, a box ambush, a cup nudged off a table, curtain climbing...), skipping anything he's done lately, so it isn't the same antic every time. He never starts a second one until someone has said something after the first, and if nobody answered within 20 minutes, the next one waits twice as long (6 hours), so a quiet channel never fills up with antics. This survives restarts. `/whiskers mischief` sets how often: off, sometimes (default) or often (up to once an hour).
- **Sometimes chimes in** on its own while roaming, based on the chattiness setting. It's more likely to when people talk about cat things like treats, fish or lasers, or when its favourite people talk. It won't take over a channel: after it says anything there, it waits before joining in uninvited again, almost 10 minutes at the default 5% and down to a minute at 100%.
- **Reacts with emoji** instead of always writing: 🐟 for food, 😹 at jokes, 🎉 for good news, 😿 when someone's sad, 🙀 for the vacuum or a dog, 💤 at bedtime, 👀 at pictures and GIFs, 😾 when someone insults it or for people it doesn't trust. Reactions need no AI, so they're instant, and they're spaced out so a busy chat isn't covered in emoji. Often a reaction replaces a chime-in completely. Turn them off with `/whiskers options reactions:False`.
- **💤 on your message** means Whiskers is still waking up or too many people are waiting. Try again in a bit.
- It ignores other bots, so two bots can't get stuck talking to each other.

## How Whiskers grows

Whiskers' personality has two layers:

1. **`personality.txt`**: the starting personality, an ordinary cat. Edit it if you want a different starting point.
2. **The journal**: a short list in Whiskers' own words about who it is now. It starts as "I'm Whiskers, a cat. I can't talk." After every 25 messages Whiskers sends (and at most once every 2 hours; `REFLECT_EVERY` and `REFLECT_MIN_HOURS` in `.env` change both), it rereads recent chats and its memories and changes one to three of its (at most 14) lines. New habits, favourite people, nicknames and running jokes show up there. What it says about people has to match how Whiskers actually feels about them, so a bath or one bad moment never becomes a grudge in the journal. Unusual abilities like superpowers only appear once the server has built toward them over time.

The update waits for a quiet moment (a minute without replies): on a CPU it takes a few minutes, and anyone talking to Whiskers interrupts it within seconds. A very active server may never have one, so once the journal (or writing memories of chat, which works the same way) has been put off for 15 minutes, it takes its turn in the reply queue like a reply would, and replies wait a couple of minutes for it. The journal goes first, because a busy server always has some chat waiting to become memories. Simulations step aside for both between replies. `/cat journal` shows when the next update is due, and `/whiskers personality evolve` does one right away.

The journal is the truth for Whiskers: whatever it says, Whiskers does. That's why it never keeps dog habits (wagging or happily thumping his tail, panting, barking) or people's talk about fixing or testing the bot: a line like that would outweigh his rules on every reply, so it's left out even if someone asks for it. You can steer it with `/whiskers personality edit`, roll back a change you don't like with `undo`, or start over with `reset`. Each server has its own Whiskers.

### Who it loves, and who it holds a grudge against

Every time Whiskers answers someone, it also notes how what they just did made it feel: loved, liked, neutral, disliked or hated, and what it was ("tuna", "belly rubs", "tail pulling"). That adds up to how much it likes each person:

- Treats, gentle petting and play make it like someone more, but only so much in one day, so trust takes a few days to earn. Only someone being mean to it makes it trust them less: hitting, kicking, pulling its tail, a water spray, throwing things, calling it names. That's checked in code, not left to the model, because the model also "disliked" things like someone leaving, saying goodnight or turning the laser off, and those used to turn friends into people it was wary of. Things it just doesn't enjoy, like a bath or the vet, are remembered as dislikes without costing the person anything. Hurt counts straight away, but one bad day can only take away so much, so a friendship survives a rough afternoon but not repeated cruelty.
- Feelings about the same thing add up: after an afternoon of loving the laser, it going off once doesn't make the laser something Whiskers dislikes.
- Before each reply Whiskers is reminded how it feels about the people in the conversation: what it loves and doesn't like with each of them overall, and whether someone hurt it in the last few hours. So it seeks out the people it loves, sulks for a while after being hurt, and keeps its distance from someone who keeps being mean to it, until they win it back with gentleness and treats.
- New people get curiosity, not fear: Whiskers sniffs them and keeps a little distance until it knows them, but it isn't scared of anyone who hasn't given it a reason.
- When it loves someone enough (and knows them well), Whiskers **chooses them as one of its people**, next to its human: it greets them happily and picks their lap. It chooses up to 3. Someone who keeps hurting it stops being one, though one bad moment doesn't undo it. `/whiskers status` shows who it has chosen.
- Its human (the server owner, or whoever `/whiskers owner` sets) is always its favourite, whatever happens. Whiskers still remembers the bath.

People who are kind to Whiskers also get chimed in on a bit more often, and people it doesn't trust a bit less.

Databases from before this check are cleaned up once on the next start: affection lost to dislikes that weren't anyone being mean is given back (the log lists who), and a grudge with no real mistreatment behind it is dropped.

### Learning to talk

Whiskers starts as just a cat. Its replies are only *actions* and cat sounds (mrrp, meow, purr, hiss) and emoji. Small models slip into English even when told not to, so any human words are also removed from its replies before they're sent.

It can learn to communicate, the way your old cat started talking through thoughts. That only becomes possible after Whiskers has been in the server for a week (`SPEECH_AFTER_DAYS` in `.env`) and has updated its journal at least 5 times. From then on, each journal update checks whether people have kept teaching or encouraging it to talk. Once they have, Whiskers learns to get simple thoughts across, and the journal says how.

You can also decide yourself with `/whiskers personality speech`: lock it back to just a cat, or let it communicate right away. `reset` makes it an ordinary cat that can't talk again. `/cat journal` and `/whiskers status` show which stage it's at.

## What it remembers

- **Recent chat**: messages in allowed channels, kept for a week. Whiskers reads the last couple dozen messages before it answers.
- **What happened earlier in the scene**: with each reply Whiskers writes a private note on the scene ("Kenobi refilled the goat milk; Whiskers isn't interested yet"), kept with the message. Once a message is too old to be in the chat it reads, the notes from before it (up to 10, back to the last long silence) are read with it, so the milk he snubbed twenty messages ago is still there when he's hungry. They stay in the model's cache with the chat, so they cost nothing per reply.
- **Memories**: while things are quiet, Whiskers turns chat into short diary entries ("Alice gave Whiskers a laser pointer and it chased it for an hour") and recalls the relevant ones when a topic comes up again.
- **People**: facts about each person (pets, hobbies, how they treat Whiskers), how well it knows them, how much it likes them, and what it loves and doesn't like with them overall.

It never reads channels it isn't allowed in, deleted and edited messages are updated in its chat log, and people can see or erase what it knows about them with `/cat memories` and `/cat forget-me`. Everything is stored in `data/whiskers.db` on your server.

## How replies stay fast

On a CPU, a reply costs two things: reading the conversation (the prompt) and writing the answer. Whiskers keeps both small:

- **Each busy channel keeps its own cache.** llama.cpp remembers what it has already read, so a reply only reads the new messages. Every channel Whiskers talks in gets its own slot (as many as `LLM_CONTEXT` allows), and background chores like writing memories use a separate one, so answering in another channel or writing memories never makes it re-read a conversation.
- **Channels that lose their slot are saved, not forgotten.** When more channels are active than there are slots, the one used least recently is saved to disk (in `data/slots`) and restored when it comes back, in a second or two instead of re-reading the whole conversation (a minute or two on a CPU). `LLM_SAVED_CHANNELS` sets how many are kept (default 4); with `nemo-12b` each takes about half a GB of disk.
- **A restart doesn't make it start over.** When the bot shuts down, the channels it has open are saved too (for up to 20 seconds), and the saved files are kept across restarts as long as the model, the llama.cpp version and the slot settings stay the same. After a restart, its prompt is copied back in a second instead of being read for two minutes, and an active channel picks up where it left off. Changing `MODEL`, `LLM_CONTEXT`, `LLM_SLOTS` or `LLM_FLASH_ATTN` starts fresh once.
- **It gets ready while nobody's talking.** Its personality, journal and rules are the longest part of every prompt. Whiskers reads them once (after its journal changes, or a restart that changed the settings) and copies them into every other slot in a second or two, so a channel's first reply only reads that channel's chat. Choosing a favourite person changes only the notes, not this part, so it costs nothing.
- **Still one reply at a time.** A CPU can't read two prompts faster than one, and writing two replies at once makes each one slower, so replies take turns; when several channels are busy, each waits for the replies ahead of it.
- **The message is posted as soon as it's written.** The model writes a private scene note, then the message, then how the person made Whiskers feel. The message goes out right away while it finishes the rest.
- **The next reply is half-read before anyone writes it.** Most of what Whiskers reads before a reply doesn't depend on the next message: the chat so far, who's around and how it feels about them, what it remembers and where the scene stands. Right after it replies, while people are typing, it reads all of that into its cache. When the next message arrives, only that message is new (about 70 tokens instead of about 260).
- **Thinking before acting is worth its time.** On a CPU, writing is the slowest part (with `nemo-12b`, about 4 tokens, or 3 words, a second). The private scene note before the message is Whiskers' moment of thought: what just happened, how it feels and what it does about it. It costs a few seconds, but it's where Whiskers' personality and its grip on the roleplay come from: when it was cut to 10 words, replies came out short, passive and almost silent. `SCENE_NOTE=after` in `.env` moves the note behind the message: several seconds faster, but blander. `MODEL=nemo-12b-fast` is the same model in a smaller file, about 10% faster.
- **Emoji reactions need no AI at all**, and in roaming channels a reaction often replaces a chime-in.

## Settings in .env

The commented `.env.example` lists them all. The ones you might change:

- `MODEL`: which model Whiskers thinks with.
  - `nemo-12b`: best roleplay and understanding of what's going on. Needs about 10 GB of RAM; about 3x slower than `qwen3-4b`.
  - `nemo-12b-fast`: the same model in a smaller file, about 10% faster with nearly the same quality.
  - `llama3.1-8b`: good roleplay, faster. Needs about 7 GB of RAM; about 2x slower than `qwen3-4b`.
  - `qwen3-4b` (default): quick, for 4 to 8 GB servers, but weaker at roleplay.
  - `llama3.2-3b` if replies are too slow, or `qwen2.5-1.5b` on a 2 to 3 GB server.

  After changing it, restart. The new model downloads once; you can delete the old `.gguf` from `models/` to free space.
- `LLM_THREADS`: CPU threads for the model. Leave it empty; Whiskers reads your plan's CPU limit.
- `LLM_CONTEXT` and `LLM_SLOTS`: memory for conversations. Each channel Whiskers keeps ready gets 6144 tokens, plus one share for background chores: `18432` with `LLM_SLOTS=2` keeps two channels ready (16 GB server with `nemo-12b`), `12288` one, and `8192` suits small servers. `30720` with `LLM_SLOTS=4` keeps four, but needs about 5 GB of RAM for conversations alone, which leaves memory tight on a 16 GB server running `nemo-12b`. Lower it if the server runs out of memory.
- `LLM_SAVED_CHANNELS`: how many channels are kept on disk to come back quickly after losing their slot or a restart (default 4, about 0.5 GB of disk each with `nemo-12b`, plus two copies of its prompt).
- `LLM_FLASH_ATTN`: `auto` (default), `on` or `off`. On a CPU, `off` writes a little faster but reads prompts about a fifth slower, and reading is most of a reply's time, so keep `auto`. The log shows each reply's reading and writing speed if you want to compare.
- `SCENE_NOTE`: `before` (default) or `after`. `after` makes replies several seconds faster but blander; see [How replies stay fast](#how-replies-stay-fast).
- `TIMEZONE`: so Whiskers knows when it's night (for example `America/New_York`).
- `CAT_GENDER`: `boy` (default) or `girl`, for the pronouns the story uses.
- `OWNER_IDS`: your Discord user ID, to use the admin commands in any server.

## Testing changes

`/whiskers simulate` plays the same scripted scenes every time, so after changing `personality.txt`, the model or the temperature you can run it again and compare the transcripts. There are 271 scenes in packs (daily life with its human, food, play, mischief, scary things, other people, staying in character when tested, magic, one long day, who it loves and holds grudges against, a lot of everyday Discord chat, and eight marathon chats), so most packs take 5 to 30 minutes.

If the AI engine restarts partway, a simulation waits for it (up to 10 minutes) and carries on. Restarting the whole server ends it, so on a host with scheduled restarts, start long packs right after one. The transcript so far is saved after every scene as `data/simulation-transcript.txt`, so a run cut short isn't lost.

The **feelings** pack starts some scenes with history (Angel almost loved enough to be chosen, Tearyu having pulled its tail before) and skips ahead in time ("--- The next day ---"), so only what Whiskers remembers about people carries over. Ordinary server chat is where Whiskers spends most of its life, so most scenes are Discord packs (all of them together: `all-discord`):

| Pack | What's in it |
|------|--------------|
| `discord` | A sampler of everything below |
| `discord-hello` | Good mornings, goodnights, coming home, leaving for school, AFK and back |
| `discord-to-him` | Talking straight to Whiskers: questions, compliments, insults, tricks, "speak", homework, advice, secrets, people replying to his messages |
| `discord-chatter` | People talking among themselves about games, movies, food, work, school, pets, birds and fish tanks, good and bad news |
| `discord-media` | Pictures, GIFs, stickers, voice clips, videos, custom emoji, spoilers, polls, code and links |
| `discord-gaming` | Looking for a group, losing streaks, wins, game night, streams, lag |
| `discord-chaos` | Ping storms, scam links, trolls, arguments a mod breaks up, bullying, someone claiming to be his owner, jailbreak attempts |
| `discord-server` | New members, rules, giveaways, holidays, server boosts, counting, dead chat, Whiskers as mascot |
| `discord-people` | How he treats people in chat: venting friends, a busy human, a second cat, old grudges, favourites coming online |
| `discord-odd` | Punctuation only, other languages, typos of his name, other bots' commands, long rants, "are you ChatGPT" |
| `discord-busy` | Long, noisy stretches of a busy channel (10 to 16 messages) with Whiskers pulled in partway, and two scenes (`kitchen-and-couch`, `who-did-that`) where several people play with him at once while chatting with each other: he decides what to answer as in a real home channel, and each person's actions must stay theirs |

Lines are written the way people really type, and links, custom emoji and pings go through the same clean-up as real messages (a Tenor link reaches Whiskers as "[sends a gif: cat vibing]"). Whiskers reads every line, sometimes chimes in (marked "chimes in") and answers when someone pings him, says his name or replies to his message (marked "replying to Whiskers").

The other scenes are a few replies long and start fresh, but real chats run for dozens of messages, and some problems only show up that far in: going quiet, falling into habits, staying in one mood too long, losing the thread, an old memory wandering into the scene. The **marathon** pack tests that with seven continuous chats of 38 to 48 replies each (about 2 hours), modelled on real home-channel chat:

| Scene | What happens |
|-------|--------------|
| `laser-marathon` | Milo plays laser for ages: the dot lands on Brandon's foot, on Angel's head and on Whiskers' own tail, gets put away and comes back |
| `evening-with-human` | A whole evening alone with his human: dinner, TV, the keyboard, play, a belly trap, bed and a 3am crash |
| `two-at-once` | Angel and Milo compete for him with treats and toys, then his human walks in |
| `rough-start` | Tearyu pokes him and pulls his tail, gets told off, then spends a long time trying to make up for it |
| `home-channel-afternoon` | People talk about him out of character, ask how he works and post GIFs, with only some roleplay |
| `vet-day` | The carrier, the vet, and a long sulk his human has to win him back from |
| `all-nighter` | A late gaming night where most of the chat isn't about him: a spilled drink, a scare, a 4am wake-up |
| `milk-and-lap` | Continuity: Milo puts out goat milk Whiskers snubs, then Whiskers naps on Milo's belly and gets into mischief (he should get down first), and twenty replies later, hungry with an empty food bowl, he should remember the milk |

Some scenes have lulls ("--- Nobody says anything for a few minutes ---") where Whiskers starts something on his own, marked "starts something"; the mischief pack's `bored-cat` and `lazy-sunday` scenes test that too.

As in real chat, every 30 messages the chat so far is turned into a memory, shown as "(writes a memory: ...)", so later replies can draw on it. Memories don't carry over between scenes.

Each line of the transcript shows Whiskers' private note on the scene, which tells you whether a bad reply came from misunderstanding the situation or from how it acted on it. Under each reply it shows how Whiskers felt about the person and its affection for them, and marks when it chooses someone or stops counting them as one of its people. Replies are checked automatically for stock fallback actions, human words that had to be removed (which words, and what the model actually wrote), "I/my" in actions, dog behaviour, touching phones, screens or links, repetition and overly long replies, with a count at the end. The scenes live in `whiskers/simulate.py`, `whiskers/discord_scenes.py` and `whiskers/marathon_scenes.py`.

The summary at the end has the numbers to compare one run with another: the average reply time (split into a scene's first reply, which reads more, and the rest), how many replies make a cat sound and how many say one out loud, the average length, how many start passively ("tilts", "looks", "stares"...), which is what the model falls back on when it runs out of ideas, and how his feelings came out: how many replies he loved, liked, disliked or hated, and how many dislikes were held against someone (marked "held against them" in the transcript), which should only be real mistreatment. When a run has long scenes, a table shows the same numbers for replies 1 to 10, 11 to 20, 21 to 30 and 31 onwards, so you can see whether Whiskers stays himself the longer a chat goes on. Real chat always goes first: a simulation pauses between replies whenever someone is waiting for Whiskers, and it uses the background cache slot so real channels stay ready.

Simulations don't change the model itself. They show where Whiskers goes wrong, and the fixes go into `personality.txt` and the rules and example replies in `whiskers/prompts.py`.

## Using a different AI server

Set `LLM_BACKEND=external` in `.env` to use any OpenAI-compatible server instead of the built-in one. For example, [Ollama](https://ollama.com) on a gaming PC with a GPU (`LLM_BASE_URL=http://your-pc:11434/v1`, `LLM_MODEL_NAME=qwen3:8b`), or a hosted API with `LLM_API_KEY`. Bigger models make a smarter cat, and memories and the journal keep working the same way.

## Troubleshooting

- **Whiskers is online but never answers.** Allow a channel with `/whiskers channel allow`, check that Message Content Intent is on, and check that its role can send messages there. `/whiskers status` shows whether the brain is ready.
- **The server keeps restarting or the AI crashes.** It's probably out of RAM. Use a smaller `MODEL` or a lower `LLM_CONTEXT` (`12288`, then `8192`). The model's own log is in `data/llama-server.log`.
- **Replies are too slow.** Check that `LLM_CONTEXT` is big enough to keep your busy channels ready (see above), then use a smaller `MODEL` (`nemo-12b` → `llama3.1-8b` → `qwen3-4b`), lower `HISTORY_MESSAGES`, or get more CPU cores. `data/whiskers.log` has a line for every reply: who it answered, how long it waited in the queue, how many words of the prompt had to be read fresh and how many were already cached, how many it wrote, and how long until it was posted. Send that file along when asking for help with speed.
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
