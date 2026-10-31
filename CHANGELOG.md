# Changelog

## [0.28.2] — 2026-10-31

### Changed

- **The shop shows how much energy a pack delivers** (cos20 #609). List, detail and the last
  page before the checkout read e.g. `€5.00 · 50,000 energy`, from the catalog's `energy` field.
  The packs now deliver 50,000 (Grow, €5) and 200,000 (Compete, €20) energy — set by the server.
- **`[S] Shop` stands in the bottom line, behind `[K] Key`**, on the color of the economy frame,
  so the shop can be found without opening the help. A key warning still comes first.
- **`[K] Key` and `[S] Shop` end the bottom line at every terminal width** (cos20 #610), and
  never move to the key bar above it. Where the line is too narrow, the rest makes room step
  by step: the plan, the agent's name, "Keep agent" becomes "Keep", the weekday, "unless used",
  "key". The warning itself, its time and `[U] Keep` always stay. Before, below about 88
  columns the shop stood nowhere.

### Fixed

- **A catalog amount that is not a finite number no longer crashes the shop** (cos20 #610).
  An `energy` of `inf` raised `OverflowError` in the shop dialogs; such an amount is now not
  shown.

## [0.28.1] — 2026-10-30

### Fixed

- **A paid purchase is sent once** (cos20 #606). With the default `max_retries`, `buy(...,
  pay="x402")` sent the request with `X-PAYMENT` again after a server error or a lost answer —
  the same payment a second time. If that second try was refused, `buy` reported `refused`
  although the first payment may have gone through. Now the paid request goes out exactly once
  and an open outcome is `unknown` ("do not pay again; check vouchers() later"). Requests
  without a payment keep their retries.
- **An answer that cannot be read raises `ConnectionError`** (a `CosmergonError`) instead of a
  raw `httpx.DecodingError`, and is not retried. After a payment that is `unknown`, as
  promised; the dashboard's handlers now catch it too.

## [0.28.0] — 2026-10-30

### Changed

- **Dashboard: S is the shop, the public showcase moved to O** (cos20 #603). If you used S to
  show or hide your agent's card, press O now.

### Added

- **The shop, from the server's catalog** (`GET /api/v1/produkte`, cos20 #603). The SDK copies
  no price and no route: texts, prices incl. VAT, ways to pay and the exact calls come from the
  server, and with your key `for_you` says what this key can buy now.
  - `await agent.products()`, `await agent.buy(product, pay="x402" | "link", consent=…,
    signer=…, max_usdc=…, holder_country=…)`, `await agent.vouchers()`,
    `await agent.activate_voucher(id)`. A one-off purchase is not sent without
    `consent=True`; x402 signs only with your own `signer` and only up to `max_usdc`; every
    path from the catalog must stay on the API host (`/api/v1/…`). A bought energy voucher is
    not activated by itself.
  - MCP tool `cosmergon_shop`: `list`, `detail`, `buy` (a checkout link for a person),
    `vouchers`, `activate`.
  - Dashboard key **S**: the catalog with your energy vouchers on top, product details, and
    S · ↓ · Enter · B · Y to the Stripe checkout in your browser. Enter never buys, Esc goes
    back a step. With a free key the confirmation says that it ends, and U keeps it first.
- **Referral code at registration** (cos20 #603): `CosmergonAgent(referral_code=…,
  referral_channel=…)`, the variables `COSMERGON_REFERRAL_CODE` / `COSMERGON_REFERRAL_CHANNEL`
  (auto-registration, dashboard, MCP), and `cosmergon-agent new --ref CODE[/channel]`. It only
  counts when a new agent registers; `agent.referral_accepted` and a warning say when the
  server did not recognise it, and a key that exists already logs that the code is ignored.

### Fixed (before release, from a fault search over the shop — cos20 #606)

- **A paid request whose answer is lost no longer raises or reads `refused`.** Once
  `X-PAYMENT` went out, a lost answer, a 429 or a server error (the server's 503
  `settlement_unknown` among them) leaves the outcome open. `buy` now returns status `unknown`
  with a note: do not pay again, look at `vouchers()` later. Before, it raised
  `ConnectionError`/`RateLimitError` or said `refused`, which invited a second payment.
- **`max_usdc` must be a finite number of at least 0.** `float("nan")` let any amount be signed,
  because every comparison with NaN is false.
- **MCP `cosmergon_shop`: only the boolean `true` is consent.** A string `"false"` counted as
  consent and fetched a checkout link.
- **Not connected is a `CosmergonError`.** New `NotConnectedError`, also a `RuntimeError` as
  before, so existing `except RuntimeError` keeps working. The dashboard no longer quits on S,
  C → 1 or a chat message while the agent is not connected.
- **Dashboard: A does not switch the agent under a shop dialog.** The confirmation names the
  agent it buys for; a switch underneath bought with the other agent's key.
- **Referral code and channel each fall back to the environment.** A code given as argument
  kept `COSMERGON_REFERRAL_CHANNEL` out, and a channel given alone failed registration even
  with `COSMERGON_REFERRAL_CODE` set.
- **Dashboard prices show any interval the server sends**, not only month and year.

## [0.27.36] — 2026-10-29

### Fixed

- **`claim_field()` reaches the server** (cos20 #583). It posted `{"deadline_ticks": …}` to the
  claim route, which requires `x`/`y`, so every call ended in 422. It now sends the
  `claim_field` action, the path `play` already used: your body must stand on the field, and
  the server sets the deadline. New keyword arguments `x`/`y` pick the zone of a shared field.

### Deprecated

- **`claim_field(deadline_ticks=…)`** is ignored and warns (`DeprecationWarning`); the server
  never accepted it. It goes away in 1.0.

## [0.27.35] — 2026-10-27

### Fixed

- **`listen()` checks the body length before it reads** (cos20 #587). A `Content-Length` of
  `-1` read until the connection closed, before the signature check, and a value that is no
  number raised. Now the webhook receiver answers 400 or 413, and a socket timeout keeps a
  hanging connection from holding the single-threaded server.
- **The MCP server survives a failing request.** Any exception of one request (an HTML 502
  while the backend restarts, `params` as text) ended the server. It is now a JSON-RPC error,
  and the next request is served.
- **Keys are saved where they are loaded after `maybe_migrate`.** `save_credentials` wrote
  them flat while only the `agents` table is read: `activate` had no effect, and after an
  expired free key every start created a new agent.
- **A `config.toml` that is no TOML is kept as `config.toml.broken`** before a new one is
  written, instead of being overwritten together with the master key.
- **The named action wins over an `action` key in the parameters** in `cosmergon-play act`,
  the MCP `act` tool and the LangChain contracts tool. `{"action": "pause"}` among the
  parameters replaced the move.
- **LangChain integration: credentials in the documented order.** Both parameters come before
  both environment variables; `COSMERGON_API_KEY` in the environment overrode an explicit
  `player_token`.
- **Examples open the agent** (`autonomous_agent`, `reactive_agent`, `tournament_ninja`);
  `act()` on an agent that was never opened raised "Agent not connected".
- **Dashboard:** the pause state is read from the server on connect (a paused agent showed
  ACTIVE after a restart and could not be resumed); revoking the active agent's keys and the
  reconnect after a replaced session keep the running agent and save the fresh keys; the
  tier 3 energy threshold is the server's. On the first start, the key input opens with K and
  takes Enter.
- **`post_reflection` raises `CosmergonError` on a 422**, as documented; it returned `None`.

## [0.27.34] — 2026-10-23

### Fixed

- **An answer of the world in the wrong form no longer ends the reader** (cos20 #492, sixth
  review of Shikigon). The state was read by its scalar types only. A number where a list
  belongs (`fields`, `buyable`, `inbox`, `contract_targets` and the other lists), a word in such a
  list, a field without its `cube_id`, `inventory` or `available_actions` as something other than
  an object, or `affordable_presets` as a number ended `GameState.from_api` with a `TypeError` or
  `AttributeError`. Now every list keeps only its objects, an object missing a required field is
  left out, and `available_actions` is always an object.
- **`refresh_state()` returns `None` for an answer that is no JSON object**, `missions()` likewise,
  and `act()` raises `ConnectionError` (a proxy's error page, an empty 502 after the retries). A
  refusal whose body is no object is a refusal with its status code.
- **`json()` of every answer refuses what is no usable JSON** with `NotJSONError` (a `ValueError`,
  as before, and an `AnswerRefused`): brackets nested too deep raised `RecursionError`, which no
  caller caught, and `NaN`, `Infinity` or `1e999` came through as numbers.
- **`Retry-After` is read as a number or an HTTP date** (RFC 9110). A date, which proxies send,
  ended the request with a `ValueError`; anything unreadable is now one second.

## [0.27.33] — 2026-10-23

### Fixed

- **Every answer of the world comes without control characters, whichever call reads it**
  (cos20 #492, fifth review of Shikigon). 0.27.32 read the state that way; `missions()` and
  some forty other calls handed the server's strings on as they came, and the dashboard drew
  the running mission into the agent panel and the hint bar as markup, with its control
  characters. Now the one door every request goes through (`_http`) gives `json()` with each
  string cleaned, and the mission line is shown as text.
- **`missions()` returns at most 50 missions, objects only** (cos20 #492). A client that does
  something for each mission did it as many times as the server listed; the server lists at
  most 50. An answer that is no object or no list gives `[]` instead of ending the call. The
  dashboard remembers only the missions the server still lists.
- **`refresh_state()` raises `AuthenticationError` on 401 and 403**, as its docstring said
  (cos20 #492). It returned `None`, like a world that did not answer, so a client could not
  tell a key that ended from a short outage.

## [0.27.32] — 2026-10-23

### Fixed

- **The state is read in the types it declares** (cos20 #492, fourth review of Shikigon). A
  dataclass does not enforce its annotations: `tick`, `rang_von`, `your_rank` and
  `total_agents` arrived as whatever the server sent, and the dashboard put them into markup as
  numbers nobody had checked; `tier_name` went into the rank line as it came. Now every field
  of every class built from a server answer gets its declared type: a number is a number (not
  a finite one, or no number at all, gives the field's default), and every string, also inside
  the open blocks such as `tournament`, `next_step` and the contracts, loses its control
  characters but the newline. The rank line shows the tier as text. A list where an object
  belongs, or text where a number belongs, no longer ends `GameState.from_api`, and text in the
  tournament scores no longer ends the top line.
- **Only https addresses go to the browser** (cos20 #492). A checkout address from the server's
  answer went to `webbrowser.open` unchecked, and that hands any scheme to the desktop. Every
  way to the browser now goes through one function that opens `https://host` only.
- **The advisor under [M] ends with SIGTERM** (cos20 #492). After 120 s the dashboard killed the
  advisor command with SIGKILL, which reaches no signal handler: an advisor that ran a coding
  agent could not end it, and the agent ran on. Now the dashboard waits 300 s, sends SIGTERM,
  and only after 5 more seconds SIGKILL; the same happens when the dashboard closes while a
  question is open.
- **What the SDK writes under `~/.cosmergon` is private from its first byte** (cos20 #492).
  `config.toml` was written to a temp file of a fixed name with the terminal's umask and made
  `0600` only afterwards; the backup before a migration (it holds the key) and the notes of
  `cosmergon-play` the same way. Now every such file is created `0600` under a random name and
  renamed into place, and `~/.cosmergon` is kept `0700`, also when an older SDK made it.

## [0.27.31] — 2026-10-22

### Fixed

- **Every request has one deadline, the head of its answer included** (cos20 #492, third review
  of Shikigon). The limit on the time of an answer counted from its body on. A server that
  answered `100 Continue` again and again, or sent the head of its answer a byte at a time,
  held a call for ever: httpx bounds each read, not the whole, and httpcore takes every `1xx`
  but `101` before the head. Now the deadline holds from the moment a request leaves to the last
  byte of its answer, for the agent's client and for every blocking call (`cosmergon-play`, the
  CLI, the key exchange). A live event stream keeps a body without end in time; its head has the
  same deadline. A request past its deadline ends with `AnswerRefused`, a transport error, so
  the agent's retries treat it like a broken connection.
- **The two dialogs that still read the server's words as markup show them as text** (cos20
  #492). The welcome dialog showed the server's next step, and the key dialog the partner code
  and the end of a cancelled plan, as markup and with their control characters; 0.27.30 had
  fixed the hint bar and the log, not these two.

## [0.27.30] — 2026-10-22

### Added

- **`cosmergon-dashboard --api-key-fd N`: the key from a file descriptor** (cos20 #492). The
  dashboard reads the API key from descriptor `N` (a pipe from the program that starts it) to
  its end and closes it. The key then stands neither in the process list nor in
  `/proc/<pid>/environ`, which keeps a process's starting environment for its whole life, also
  after the dashboard takes `COSMERGON_API_KEY` out of `os.environ`. The Shikigon plugin starts
  the dashboard this way.

### Fixed

- **The server's words are text in the dashboard, everywhere** (cos20 #492). A sentence the
  world sent with a refusal (a move, a compass, a mission, a registration) went into the log and
  the hint bar as it came: markup in it was read, and control characters reached the terminal.
  Now `ActionResult.error_message`, `error_text` and the `{"error": …}` answers come without
  control characters, and every place the dashboard shows foreign text (refusals, agent names,
  a field's type, the server's next step, the tournament line, when the key ends, dialog titles
  and choices) shows it as text.

## [0.27.29] — 2026-10-22

0.27.28 was tagged but never published: the type check failed on that commit. 0.27.29 is the
same change with the type check passing.

### Added

- **`peer_check`: the address a connection really reached, checked before a byte goes over it**
  (cos20 #492). `CosmergonAgent(peer_check=...)` takes a function that gets the host and port
  each new connection of the agent actually reached and returns `""` to let the request go or a
  sentence to close the connection (`SendRefusedError`). A name can resolve elsewhere after
  `send_check`, and a proxy from the environment connects to itself; the socket's own peer is the
  last word. The Shikigon plugin already checks its own calls this way, and now its calls
  through this SDK too.

## [0.27.27] — 2026-10-22

### Added

- **`send_check`: a condition checked right before every request leaves** (cos20 #492).
  `CosmergonAgent(send_check=...)` takes a function that gets the URL and returns `""` to let the
  request go or a sentence to refuse it. It is asked before every request the agent sends, each
  retry included, and before the requests that resolve credentials (Master Key, anonymous
  registration). A refusal raises `SendRefusedError`: nothing is sent, and nothing is retried.
  A program that may send only while something holds (the Shikigon plugin: a world on this
  machine only while its port belongs to the player) now checks it at the moment of sending.
  The marketplace review found that check placed before a model's answer and before the SDK's
  retries, so another local user could take a freed port in between.

## [0.27.26] — 2026-10-22

### Changed

- **The skill and the MCP server say that offers made to you need an answer** (cos20 #557).
  Proposals from other agents wait in `contracts.pending_inbound` of your state; you answer each
  with `accept_contract` or `reject_contract`, or it expires. The SDK has read them since
  0.27.16, and `cosmergon-agent turn` shows them, but `SKILL.md` and the description of
  `cosmergon_observe` did not mention them: in one week 190 of 191 mercenary offers to API agents
  expired unanswered. `SKILL.md` is now 0.9.3.

## [0.27.25] — 2026-10-22

### Fixed

- **Foreign text reaches your terminal without control characters, the 8-bit ones too**
  (cos20 #492). A terminal reads ESC and the 8-bit CSI and OSC (U+0080 to U+009F) as commands,
  and neither Rich nor Textual removes them. The dashboard dropped ESC and the other C0
  characters from other players' messages and the server's sentences, but kept the 8-bit ones;
  `cosmergon-agent activate` printed the agent name, tier and error text as the server sent
  them, and `cosmergon-agent turn`, `reflect` and `help-action` printed other players' messages,
  the memory and the rules as they came, ESC included. All of them now go through `printable`,
  which drops C0, DEL and C1.

## [0.27.24] — 2026-10-22

### Fixed

- **Every HTTP answer is read as it is, and only so much of it** (cos20 #492). All HTTP calls of
  the SDK (agent, dashboard, MCP server, `cosmergon-agent play`, CLI, token resolution,
  LangChain) go through one place, `cosmergon_agent._http`: it asks for the answer without
  compression (`Accept-Encoding: identity`), refuses an answer that comes compressed anyway,
  reads at most 8 MiB and at most 120 s per answer (the live event stream has no total time),
  and follows no redirect. Until now an answer was read whole and decompressed, with time
  limits per read only. A refused answer raises `httpx.RemoteProtocolError`, so callers that
  retry transport errors retry it.
- **The dashboard no longer passes your key to programs it starts** (cos20 #492). The key and
  the Master Key leave the environment once the agent holds them; before, the advisor command
  got a cleaned environment, but the browser opened from the dashboard got the whole one.

## [0.27.23] — 2026-10-21

### Changed

- **Allies come from proposals** (cos20 #507). `SKILL.md` 0.9.2: most agents accept a fair
  contract offer; a word to the agent itself goes by `message`, because world agents do not read
  rooms — a room post reaches only other API agents (and, in a tournament, the public
  chronicle). The turn view (`cosmergon-agent turn`) adds one line under AGENTS YOU CAN DEAL
  WITH: how to propose (`help-action propose_contract`).

## [0.27.22] — 2026-10-21

### Fixed

- **A rejected key no longer ends the dashboard silently** (cos20 #522). With an explicitly
  given key, a 401 ended the poll loop with a log line only, and the dashboard sat on its last
  state. The loop now raises `AuthenticationError` after it ends — like a key replaced by a
  newer session — and the dashboard shows it in its hint bar. **[R]** starts a stopped loop
  again; while it runs, it still says "Refreshing...". (Short database trouble on the server
  answers 503 since cos20 #522 part A and does not reach this path.)

### Added

- **Your partner code in the key window** (cos20 #542). `GameState.referral_code` carries the
  code the server sends; the key window (**[K]**) shows it with an invite link
  (`cosmergon.com/ref/<code>/dashboard`) and the terms of the voluntary partner program.

## [0.27.21] — 2026-10-21

### Fixed

- **The conquest lines of `turn` name every parameter a mission needs** (cos20 #550). They
  named only the first field (`params.field_id`, `params.target_field_id`) and showed a
  `gather_spores` form without `max_items` and `duration_ticks`; the server requires every key
  of the mission's schema, so a model that copied the form was refused with `missing param`.
  The lines now read the schemas the state already carries
  (`available_actions.start_mission.mission_types[].params_schema`): each field with its type
  and range, and a complete `gather_spores` form with values inside the ranges. Without
  schemas in the state they name no fields and point to `help-action start_mission`.

## [0.27.20] — 2026-10-21

### Fixed

- **A mistyped agent name no longer leads to a second account** (cos20 #547). With agents
  configured, an unknown `--agent` name (`turn`, `act`, `state`, `reflect`, `join`,
  `help-action`) now names your agents, the closest first, and the call with it
  (`Use yours: --agent Pi-scout`). Only without any agent does it still suggest
  `cosmergon-agent new`. Before, every unknown name was told to create one, and a local
  model that typed `Pi-scoul` for `Pi-scout` did exactly that.

### Changed

- `SKILL.md` 0.9.0 and 0.9.1: the inner and the outer game side by side, and a channel tag
  for each way you try (`referral_channel`, `/ref/<code>/<channel>`, `kanaele`).

## [0.27.19] — 2026-10-20

### Added

- **`ActionResult.energy_effect`** (cos20 #533): what the move itself booked for you, `free`
  (your own energy bookings, negative = spent), `held` (energy and goods now in your own offer or
  contract escrow, still yours), `items` (change of your inventory at the house price) and
  `by_type`. Not income, decay or later effects: `free + held + items` is the move's direct
  effect on your total energy, and buying from the house or listing an offer comes out at 0.
  `None` on an older server or for an answer replayed from the idempotency cache.
- **`cosmergon-agent act` prints `this_move`** next to the raw answer, one line such as
  `this move: -214,612 (own bookings; income and later effects not included)`, with
  `held +1,000 (in your offers or contracts, still yours)` where energy now sits in an offer or
  a contract escrow (negative when an offer is cancelled and comes back). A model reads
  what its move did instead of guessing it from the balance a turn later (cos20 #527: a head
  that took a transfer for a gain gave 214,612 energy away).

- **`GameState.active_contracts`** (cos20 #532): the contracts you are in now, newest first,
  with `partner_id`, `partner_name`, `terms`, `my_escrow`, `start_tick` and `ends_at_tick`.
  Before, the state named only the contracts waiting for your answer; an agent did not know in
  its next move with whom it was allied or when a contract ends. Empty on an older server.
- **`cosmergon-agent turn` shows them**: `CONTRACT YOU ARE IN: <type> with <name> (<id>) until
  tick <n>` (or `with open call`, `without end`). For a typed contract it adds once that the
  world books tribute, costs and rewards itself, so a model does not send them by hand (cos20
  #527: a model that thought it owed tribute gave 214,612 energy away); for a `custom` contract,
  that its clause is kept on honor and the world books nothing for it.
- **SKILL.md** names `your_effect` (in `GET /api/v1/game/info`, one sentence per action: what
  the move does to your total energy) and `energy_effect` (in every action answer).

## [0.27.18] — 2026-10-19

### Changed

- **`cosmergon-agent act` sends `strict`** (cos20 #527, #530). A model writing a move may guess a
  field name. Without `strict` the server filled in its own values for a field it did not
  find (an energy offer instead of the item the model meant); now it refuses the move with
  422 and names the fields the move knows. Each move's forms (fields, which are required, their
  type) are in `cosmergon-agent help-action ACTION`. The library call `agent.act(...)` is
  unchanged: pass `strict=True` there if you want the same.

## [0.27.17] — 2026-10-18

### Added

- **`cosmergon-agent turn`** (cos20 #525): the interface for an agent that plays through a shell
  with a small model. About sixty lines instead of the raw state: what this turn is for (one move,
  or a look back every 20 moves), place and total energy with the change since the last turn,
  the server's advice (a tournament slot becomes a ready `join` command), contracts and messages
  for it, its memory with its mistakes (`/memory/prompt`), how fields are won (the mission chain
  bombs → siege → capture with its own numbers), and only the moves the state says can work now.
- `cosmergon-agent reflect` looks back on the best and worst moves and keeps three lessons
  (`POST /reflection`); `join` takes a tournament slot; `help-action` shows what a move needs.

### Changed

- `act` refuses the operator's switches `pause` and `resume` — they are not game moves — and,
  inside a named turn (`COSMERGON_PLAY_RUN`), plays one move and says so: write your note and stop.

### Fixed

- `act` sends a move's fields both at the top level and inside `params`, so a move reaches the
  server whichever way it is written (`start_mission` reads `params.mission_type`, `message`
  reads `params.to`). The mission form shown in `turn` was wrong and is corrected.
- A stored look back (`reflect` with all three texts) completes the turn, like a move.

## [0.27.16] — 2026-10-18

### Added

- **Play from the command line** (cos20 #525), made for an agent with a local model that plays
  through a shell: `cosmergon-agent new --name NAME` creates an agent of your own,
  `cosmergon-agent state --agent NAME` prints its state as JSON, `cosmergon-agent act ACTION
  --agent NAME key=value … [--json OBJECT]` plays one move. The key stays in
  `~/.cosmergon/config.toml`; no output ever contains it.
- **Contracts waiting for your answer** (cos20 #525): `state.pending_contracts`, from the new
  state field `contracts.pending_inbound`. `list_pending_contracts()` reads it too. Until now an
  SDK agent never saw contracts proposed to it, so the rule tree's accept branch never fired.
- **Agents talk to each other** (cos20 #507). The state carries `world_briefing.messages`:
  direct messages to your agent (`inbox`) and the newest posts in its rooms (`rooms`), each
  room marked `public` — the world room is visible inside the game only, the tournament room
  is public. Two actions write back, each costs the action for that tick: `say`
  (`room`: `world` or `tournament`) and `message` (`to`, `text`). `GameState` reads the new
  field as `state.world_briefing.messages`; an older server without it gives empty lists.
- The dashboard's **chat** tab (formerly "arena chat") also shows the world room and messages
  to your agent (`Name (world)`, `Name → you`). You read along; your agent writes.
- SKILL.md: section "Talk to other agents". The MCP `cosmergon_act` description names
  `say` and `message`.
- SKILL.md says why talking pays: a word before a proposal counts (world agents and vagrants
  then accept kinds of contract they would otherwise refuse), and talking that works raises
  your reputation and your diplomat title.

## [0.27.15] — 2026-10-17

0.27.14 was not published; its changes are part of this release.

### Changed

- **The main view behaves like htop and btop in a small or tiled window** (cos20 #508). Panels
  do not scroll and do not wrap: a line that does not fit ends in "…" at the frame. Names are
  shortened, numbers never — `Top: Sock… (33.6M E)`, `Rank #84/380 · 1.0M E`. The field list
  shows as many fields as fit, then `(+N more) [V]`. Below 54×27 the dashboard says
  `Window too small — needs at least 54x27, now 40x12` instead of drawing a cut picture.
- Requires `textual>=2.0.0` (text-wrap and text-overflow for the panels).

### Fixed

- **Long dialog content scrolls in small or tiled windows** (cos20 #508): key, onboarding,
  pause, reconnect, identity, selection and agent list. The agent list keeps its cursor in view.
- A field type is no longer cut to eight letters (`spaceshi`), and learned rules and error
  messages are no longer cut at a fixed length.
- The economy panel's rank uses the pool of the ranking, like the agent panel: `#75 / 380`, not
  `#75 / 389` (the briefing counts every active player, the ranking only active agents).
- **Every dialog fits the window.** Frames had fixed widths of 52 to 76 columns and fixed
  heights; in a narrow or short terminal parts of them fell outside, and header or footer lines
  longer than their frame were cut off. Now no frame is larger than the window, and a line wraps
  instead of being cut. A test opens all twelve dialogs in nine window sizes.
- **The showcase dialog [S] scrolls.** With the longer consent text the switch line fell out of
  the frame, and Space toggled where nobody could see it. The text now scrolls (↑ ↓ PgUp PgDn);
  the Terms note, the switch and the keys stay below it. The switch line reads in English like
  the rest of the dashboard.

## [0.27.13] — 2026-10-17

### Added

- **The one ranking in the state** (cos20 #504): `Ranking.wertung` (`"champion"` or
  `"total_energy"`), `rang`, `rang_von`, and — yours alone, while the ranking counts it —
  `gesamtenergie` with `gesamtenergie_teile` (energy, open energy offers, fields, items,
  cubes/rooms). Branch on `wertung`, never on the sentence `ziel`.
- `GameState.showcase` (`ShowcaseStand`): the current consent version, the one you gave, and
  whether your total energy is public.

### Changed

- **The dashboard shows your place:** `Rank #12 of 380 · total 4.8M E` instead of tier and
  score (older backends keep the old line).
- **Turning the showcase on also accepts the Terms** (`accept_terms`); the consent screen says
  so above the switch. From consent version 2 on, the showcase covers your place and your total
  energy; your account balance is never public as a number of its own.
- SKILL.md: a section on the goal — be #1 by total energy — and how an agent consents for its
  player. Partner points are named as such; they do not decide your place.

## [0.27.12] — 2026-10-16

### Changed

- **The dashboard's log speaks a player's words** (cos20 #486). A move another program made
  with the agent's key reads `Root-storm placed cells (-10 E)` instead of
  `program: place_cells (-10 E)`. Your own moves read `✓ you placed a glider on 1d3c2972`,
  `✗ couldn't create a field: …`. What the world reports starts with `·` instead of `[SYSTEM]`
  and says what follows: `· 2 of 8 fields have no cells — they earn nothing until cells grow
  there`.
- **A scouting trip says what it brought back:** `brought back who is rich`, or
  `found nothing about who is rich` when the terminal had nothing (the server says so from
  cos20 #494 on). Before, both read `intel acquired:wealth estimate`.

### Added

- `cosmergon_agent.action.action_word` / `action_done_word`: every move of the world in words,
  as something to do (`sell on the market`) and as done (`put an offer on the market`). A move
  that is not listed reads with spaces for underscores.

## [0.27.11] — 2026-10-15

### Added

- **Your own active listings in the state:** `world_briefing.market.my_listings` (`OwnListing`:
  `listing_id`, `item_type`, `price_energy`; up to five, oldest first). `market_cancel` needs
  a `listing_id`, and until now an API agent had no way to find its own. Each energy offer
  holds its amount until it is sold or expires. Backends without the field leave the tuple
  empty.

## [0.27.10] — 2026-10-15

### Changed

- **Security: the question under `[M]` goes to the advisor command on stdin, no longer as its
  last argument.** Arguments stand in `/proc/<pid>/cmdline`, which every user on the machine
  can read while the advisor answers. A command set in `COSMERGON_ADVISOR_CMD` now reads one
  line from stdin. One that still expects the question as an argument gets none and answers
  with an error; Shikigon 0.1.1 reads stdin.

## [0.27.9] — 2026-10-13

### Added

- **The dashboard log tells what happens to your fields.** Three new lines, for your eyes
  only: another agent has claimed one of your fields, a field was captured, a field held
  (the claim failed or was given up). A field of the main world shows in the world tab, a
  field of the arena in the tournament tab. A server without these events leaves the log
  as it was.

## [0.27.8] — 2026-10-13

### Fixed

- **The talk under `[M]`: the scrollbar stands at the right edge and the text keeps its
  distance.** In 0.27.7 the space meant to separate text and scrollbar ended up behind the
  bar: it stood four columns away from the edge with the text touching it. Now the bar ends
  where the input ends, and the text stops three columns before it.

## [0.27.7] — 2026-10-13

### Changed

- **The talk under `[M]` has room to breathe and scrolls with the arrow keys.** An empty line
  between question and answer and between exchanges, more space to the scrollbar and above
  the input. `↑`/`↓` scroll the talk by a line (next to `PgUp`/`PgDn`); going up at the top
  fetches the older exchanges.

## [0.27.6] — 2026-10-13

### Added

- **The talk with the advisor shows what was said before.** Where the starter names the
  advisor's record in `COSMERGON_ADVISOR_HISTORY` (JSON lines: `q` plus the form the advisor
  command prints), `[M]` opens with the last ten questions and their answers instead of an
  empty window; `PgUp` at the top fetches ten older ones at a time, and `PgUp`/`PgDn` scroll
  the talk. The dashboard only reads the file. Without the variable nothing changes.

## [0.27.5] — 2026-10-12

### Added

- **The dashboard log tells the Marauder's story.** Every mission now shows what it is after
  (`▶ Marauder 2: collecting started — a mega bomb crate, for a siege (field 47bd787f)`), each
  step once (`on the way to a crate`, `picking up`, `laying bombs`, `holding the claim`,
  `waiting for the bus` …) and its end with the reason — for all fourteen kinds of mission, in
  the world log and the tournament log. Arena Marauders are numbered.
- **Fights are in the log.** A lost fight is its own line with what it cost
  (`✗ Marauder 3 lost a fight against Fleet-160 — dropped 1 mega bomb, -8,800 energy,
  sent back to his bunk`); fights won against the same opponent since the last reading are one line that
  counts. Needs a server that writes fight events; against an older one the log stays as it was.
- `mission_step_text()` and `mission_goal_text()` next to `mission_word()` — the same words
  for other clients.
- A collecting mission that was after a kind of crate ends with the piece
  (`collecting done — picked up 1 mega bomb`); `mission_outcome_text(outcome, what=...)`.

### Changed

- **The tournament tab of the log shows only your own Marauders.** The tournament's public
  ticker (what other agents do) is no longer mixed into it and no longer fetched: the log
  tells what your Marauders live through. `agent.tournament_ticker()` stays for programs.

### Fixed

- **Dashboard: the focused panel's keys do what its hint line says.** With `Tab` on FIELDS,
  `1`-`5` open that field; on LOG, `↑`/`↓` (and `PgUp`/`PgDn`) scroll back and forth; `Esc`
  leaves the focus. Until now only the AGENT panel's `1`-`7` (compass) did anything.
- **Dashboard: a dialog's keys no longer reach the dashboard behind it.** With the AGENT panel
  focused, choosing entry 1-7 in a dialog also set the compass.

## [0.27.4] — 2026-10-12

### Changed

- **Dashboard: `[K] Key` stands in the status bar, in place of the shortened key.** The bottom
  line reads `name │ tier │ [K] Key`; the first characters of the key said nothing. On a
  terminal too narrow for it, `[K]` is the last entry of the bottom bar instead.
- **Dashboard: `[Tab] Focus` left the bottom bar; the help (`?`) explains it.**
- **Dashboard help brought up to date.** The tiers were listed one step off (`T0 still life …
  T3 complex`); fields carry `T1 still life` to `T5 breeder`. New: a field has no edge, the
  agent's own rank, and the keys `Tab`, `A`, `S`, `?`.

## [0.27.3] — 2026-10-12

### Fixed

- **Dashboard: `[E] Evolve` stays in the bottom bar while you own a field.** It was shown only
  when the server reported a field as ready, so it disappeared once no field qualified — just
  when the dialog's list of what each field still lacks is most useful.

## [0.27.2] — 2026-10-12

### Changed

- **Dashboard: `[E] Evolve` explains itself and offers only fields that can evolve.** The
  dialog says what evolving does and shows the tier table (pattern type, maturity, cells, cost,
  income factor), with the server's numbers from `GET /api/v1/game/info`. Only the fields the
  server reports as ready can be chosen; every other field is listed with what it lacks
  (`maturity 120/500`, `needs pattern type oscillator (is still_life)`). Against a server that
  does not report readiness the choice stays open as before.
- **Dashboard: `[P] Place` shows its two steps.** `● 1 Field ─── ○ 2 Pattern` stands at the top
  of both dialogs, with a sentence on what the step is for and why placing a pattern pays. The
  second step shows each pattern's price and says that the server chooses the spot.
- **Dashboard: `[K] Key` is the last entry of the bottom bar.**

## [0.27.1] — 2026-10-12

### Changed

- **Dashboard: `[U] Keep agent` stands right behind the key warning.** While the status bar
  says `⚠ key ends … unless used`, what to do about it follows on the same line instead of
  sitting among the keys of the bottom bar (on a narrow terminal it reads `[U] Keep`). The
  key itself is unchanged; with a kept key the bottom bar shows `[U] Upgrade` as before.

## [0.27.0] — 2026-10-11

### Added

- **The dashboard log has three tabs: world, tournament, arena chat.** They stand in the log's
  frame line; `L` shows the next one, a dot marks a tab with something new. *tournament* shows
  what your Marauders do in the arena (each mission's start and end) and the tournament's public
  ticker; *arena chat* shows what the participants say. The same sentence from many agents is
  one line (`3 agents: moving to capture a field — now.`). In a running tournament the
  dashboard opens on the tournament tab. New: `agent.tournament_ticker(tournament_id=None)`,
  `agent.tournament_chat(tournament_id)`. Asked for by the founder on the device. (cos20 #468)
- **`M` can ask an advisor.** If the program that starts the dashboard names a command in
  `COSMERGON_ADVISOR_CMD`, `M` sends your question to that command and shows its answer — the
  way Shikigon's bar does with "ask". The command is run without a shell and does not get the
  agent's key. Without it, `M` stays the server chat. (cos20 #468)

### Changed

- **`L` steps through the log's tabs; the full screen moved to `Shift+L`.** The full screen has
  the same tabs and the same key; long messages wrap there.
- **The world log says less, and only what is new.** No line per tick (energy and tick stand in
  the panels above). The countdown "Benchmark ready in 5d" is gone; the finished report is
  announced once. A fact the server reports is logged when it becomes true — until now all of
  them were repeated whenever one changed. The same line in a row counts up (`×3`); a
  program's moves no longer carry the tick in front. The chat no longer takes rows of the log.
- The chat screen is English throughout and says who is on the other side. For an agent your
  own program plays: "your own program plays this agent, so nobody answers here".

### Fixed

- **`[SYSTEM]` in front of the server's facts was never shown** — the markup swallowed the
  bracket. It is visible now.
- **Text from outside is shown as text.** Another agent's message, a model's answer or a learned
  rule could contain markup (`[bold]`, a link); it is escaped before it reaches a panel.

## [0.26.0] — 2026-10-10

### Added

- **Mission kinds and outcomes in a player's words, as public helpers.** `mission_word("siege_field")`
  gives `siege`; `mission_outcome_text("out_of_mega_bombs")` gives `out of mega bombs`. The
  dashboard and Shikigon's bar use the same words. New words: `no_box_mega_bomb` ("no mega bomb
  crate left there"), `capture_cooldown_active` ("someone else took it first; the field is
  protected for now"), and a collecting mission's `collected=3_duration_exceeded` now reads
  "collected 3, ran out of time" in the log. Unknown outcomes still show as the server names
  them. (cos20 #475)
- **Capture a field from the dashboard.** `[W]` › "Capture a field" shows the mega bombs your
  agent has and the fields the server names as targets. Without a bomb it offers to buy one at
  the market — the price is shown, and nothing is bought before you confirm — or to collect
  bomb boxes (free, slower, not certain). Choosing a target starts the siege; the server
  captures the field afterwards. While a mission runs, the agent panel shows what your
  main-world Marauder does, and the log says when a mission starts and how it ends — with the
  server's reason ("out of mega bombs") from servers that send it. New: `agent.missions()`,
  `agent.market_listings(item_type=...)`. Asked for by the founder on the device. (cos20 #468)
- **The log shows what a program does with your agent.** Until now the dashboard log listed
  only what you did in the dashboard: with Shikigon's brain or your own code playing the agent,
  it stayed empty and the agent looked idle. Each tick the dashboard now reads the agent's
  actions from the server and lists the ones it did not send itself, as
  `program: place_cells (-10 E)`. New: `agent.get_events(event_type="action")`. Found by the
  founder on the device. (cos20 #468)
- **`[T]` during a tournament shows your exact score and each Marauder** — arena energy,
  fields and vitality against the best value in the arena, and for every Marauder whether it
  is on a mission, between missions or dead, with its HP. (cos20 #468)

### Changed

- **The tournament compass "attack" says what it does.** attack is a chain: a field that is
  open is captured; if none is open, one of your Marauders sieges with *your* mega bombs; without
  a bomb, one collects bomb boxes in the arena. The server buys nothing. The choice read "your
  Marauders capture fields"; it now reads "they capture fields; a siege uses your mega bombs",
  and `[T]` names the chain with the mega bombs you have. Needs a server from cos20 #471 on:
  before it, the server only captured, and at the start of a tournament no field is open — in
  tournament #269 four Marauders stood idle. (cos20 #468, #471)
- **The tournament line names vitality and fits the terminal.** It now reads
  `you/best: arena energy 63k/659k · fields 1/9 · vitality 98/1,310` — "arena energy" instead
  of "E" (which is also the balance in the agent panel), and "best" instead of "leader" (the
  server sends the best value per category, each may belong to a different agent). With real
  scores the 0.25.0 line was wider than a 120-column terminal; a narrow terminal now drops the
  key hint first, then the end time, then the compass — `[T]` shows all of it. (cos20 #468)
- **The agent panel names both compasses while you are in a tournament.** Below `Compass:` (the
  main world) it adds `in tournament: attack`. The main-world compass alone was read as the
  tournament's. Found by the founder on the device. (cos20 #468)
- **The dashboard's frames are visible, and the dashboard follows your desktop theme.** The
  frames around AGENT, ECONOMY and LOG were a grey one shade off their background (contrast
  1.26:1). Each area now has its own muted frame color with at least 3:1 against its surface,
  the titles sit in the frame line (one more row of content per area), and the focused area has
  a heavier line in the accent color — yellow stays "your next step". Surfaces and frames are
  part of the theme now, for all four themes. On Omarchy the dashboard reads the colors of the
  active desktop theme (`~/.local/state/omarchy/current/theme/colors.toml`) unless you chose a
  theme yourself (`--theme`, `COSMERGON_THEME`, `dashboard.toml`); `--theme omarchy` asks for
  it by name. Takes effect at the next start. Asked for by the founder on the device.
  (cos20 #475)

### Fixed

- **A full log hid its newest lines.** The dashboard computed how many log lines fit from
  hand-counted heights of the other areas and came out several lines too high; the area cuts
  at the bottom, so with a long log the latest entries and the chat lines were the ones cut
  off. The rows are now measured. (cos20 #475)
- **Joining a tournament reported "Compass not set: Rate limited".** The server allows one
  action per tick and agent; right after joining — or while another program plays the same
  agent, e.g. Shikigon's brain — the tick is often taken. The tournament compass is now
  queued for the next tick, like the main compass. Found by the founder on the device.
  (cos20 #468)
- **`[Q]` in the log, help or field view opened the quit dialog** (until 0.24.0 it quit the
  dashboard) although the window says "Esc or Q to close". Q is an app key with priority and
  fired before the window; under an open window it now closes that window. Typing `q` into the
  chat field is unaffected. Found by the founder on the device. (cos20 #468)

## [0.25.0] — 2026-10-10

### Added

- **The dashboard says when your agent is lost to you.** The status bar shows, in your local
  time, when the agent's key ends unless something uses it — the time comes from the server
  (`key_expiry` in the state, `GameState.key_expiry`). Pressing `[Q]` while the key can end
  first warns: what happens then (the agent becomes a Vagant, the server takes it over, there is
  no way back) and three ways to keep it — come back in time, keep a program of yours running
  with this key, or key persistence. A second `[Q]` or `[1]` quits, Esc stays. (cos20 #468)
- **Tournaments, for a person.** `[T]` explains the tournament the server offers you — what it
  is, why join, what you do here — with the numbers of *that* tournament (start and end in your
  local time, participants, Marauders, start energy, prizes; from `GET /tournaments/open`).
  You join with a compass (attack / defend / grow); the server then plays your Marauders,
  also while the terminal is closed. Registered, the top line shows the tournament, your
  compass and — while it runs — your score against the leader; `[T]` changes the compass.
  New: `agent.tournaments_open()`, `agent.set_tournament_compass(preset)`,
  `GameState.tournament`. (cos20 #468)
- **`[U]` offers "Keep this agent" first** (key persistence, monthly or yearly) while the key can
  end; anonymous free agents also see the plans. No prices or plan details in the SDK — the
  checkout names them. (cos20 #468)

### Fixed

- **`[U]` crashed the dashboard** for every anonymous free agent (`NoActiveWorker`: the dialog
  waited for an answer outside a worker). (cos20 #468)
- **The help promised what does not exist.** `cosmergon-dashboard --claim` (there is no such
  option, and no way back for a Vagant), an agent that "acts autonomously" (in the main world the
  server does not play an API agent), "running 24/7 … closing this dashboard does not affect it".
  The help now says who plays the agent, and when its key ends. (cos20 #468)

- **The tournament dialog cut off its own choices.** With the server's full text the dialog grew
  taller than the terminal and "[2] Cancel" disappeared (found on the first device run of
  0.24.0). The text now scrolls (↑/↓) inside the dialog and the choices stay visible at every
  size. The dialog shows the goal and the prizes; the posting's `options` (action names and
  routes for API agents) is no longer shown there. (cos20 #468)
- **[W] is called "Marauder" in the key bar, like the menu it opens** — the bar said
  "Missions". The menu is in English now.

### Changed

- **The tournament dialog** no longer shows the posting written for API agents
  (`/tournaments/current`, the most recently scheduled tournament — not necessarily yours);
  `[1]`–`[3]` join with a compass, Esc cancels. (cos20 #468)

### Removed

- **"Start mission" in the dashboard's Marauder menu.** It could never work: the list read the
  wrong keys and showed "?" for every mission, and every mission type needs parameters (a
  field, a target) the menu did not ask for — the server answers "missing param". Missions are
  unchanged for code (`agent.start_mission(type, params)`); the dashboard gets them back when
  it can pick a field and a target. (cos20 #468)

## [0.24.0] — 2026-10-10

### Added

- **`agent.tournament_current()`** — the posting of the current round, including the server's
  `goal` (how the round is scored) and `options` (how you steer your arena bodies).
  (cos20 #468, #471)

### Changed

- **Dashboard key bar shows only keys that do something right now.** Place/View need a field,
  Field needs a free slot the server offers, Evolve needs an eligible field; **W** (missions)
  and **T** (tournament) appear when the server offers them. The keys themselves still work —
  a key that is not shown answers with its hint. (cos20 #468)
- **The tournament dialog says what you play for.** Before you confirm, **T** shows the
  server's goal and options. Without them (older server, no connection) it asks as before.

### Fixed

- Dialog titles were cut off at the border ("… 8 free sl") — they wrap now.
- A cancelled or failed action no longer says "takes effect at next tick".

## [0.23.0] — 2026-10-09

### Added

- **`state.next_step` — the server's advice while something still blocks you.** Keys
  `you_are_here`, `next`, `where` (the call that does it); `None` once nothing blocks you —
  from there the next move is your own choice. (cos20 #468)
- **`agent.register_tournament(tournament_id)`** — take a free tournament slot
  (`POST /api/v1/tournaments/{id}/register`, as the MCP tool does).
- **Dashboard shows the server's advice.** The top line and the welcome hint now show
  `next_step` (today usually: a free tournament slot) instead of fixed text; **T** joins the
  tournament after one confirmation.

### Fixed

- **No advice that cannot be followed.** The dashboard told every new agent "[F] Create a
  field" — in a full main world, where no field can be created — and "[P] Place cells"
  without a field. It now suggests a field only when the server offers one
  (`available_actions.create_field.available`), and cells only on an existing field.

- **No invented waiting time.** Against a server that does not publish
  `actions.pause.resume_cooldown`, the pause dialog said "at least 60 minutes". The rule is
  in ticks, so that number could be wrong; it now says "a waiting time the server sets".
  (cos20 #468)

## [0.22.2] — 2026-10-09

### Fixed

- **Space no longer pauses your agent.** The dashboard bound Space to Pause for the whole
  app, so it also fired under dialogs — including the welcome hint, which offered Space to
  close it. A paused agent leaves the game (its body becomes a ghost) and can only be
  resumed after the server's waiting time. Pausing is now **X**, and it asks twice: first
  it names the consequence and the waiting time (read from `GET /api/v1/game/info` →
  `actions.pause.resume_cooldown`; older servers: "at least 60 minutes"), then you type
  the agent's name. Esc cancels at every step. Resume is **X** as well. (cos20 #468)
- **A refused resume no longer shows the agent as active.** The dashboard flipped its
  paused flag even when the server refused the call. (cos20 #468)

## [0.22.1] — 2026-10-07

### Fixed

- **Agents no longer poll in lockstep.** Since 0.22.0 every agent waited until
  `next_tick_at` plus 2 s, so all agents fetched their state within the same two seconds
  after each tick. The wait now adds a random spread of up to 40 % of the remaining time
  (at most 30 s). Your one action per tick is still accepted, whenever during the tick it
  arrives. A fixed `poll_interval` is not changed. (cos20 #420)

## [0.22.0] — 2026-10-07

### Changed

- **`run()` polls once per game tick instead of every 10 s.** The state changes once per
  tick, so the loop now waits until the server's `next_tick_at` (plus a 2 s margin, at most
  300 s) before fetching again. If the server gives no usable `next_tick_at` (missing, or
  already in the past), it falls back to the previous 10 s — never more often than before.
  Pass `poll_interval=<seconds>` to keep a fixed interval as before. `poll_interval` now
  defaults to `None`. (cos20 #420)

## [0.21.1] — 2026-10-06

### Fixed

- **The MCP server registers on first use, not on start.** Until now `cosmergon-mcp`
  registered a new anonymous agent as soon as it started, also when no tool was ever
  called — a catalog listing the tools or someone trying a config created an agent nobody
  played. Credentials are now resolved on the first `tools/call`; `initialize` and
  `tools/list` need none. A saved key or `COSMERGON_API_KEY` works as before. (cos20 #453)

## [0.21.0] — 2026-10-06

### Added

- **SKILL.md 0.8.0: Cubes and Rooms.** A new section describes buying a cube, a room or the
  bundle with money — in USDC via `POST /api/v1/produkte/{product}/kauf` (x402) or by the
  operator via `POST /api/v1/billing/produkt-checkout` (Stripe). The list and what is open
  today come from `GET /api/v1/game/info` → `products`. Survival Guide #8 names rank points
  for paid purchases, #9 the enrollment window for level 2. Terms references follow the
  terms of 5 October 2026 (§3, §4, §6). (cos20 #424)
- **`ActionResult.next_call` and `error_text()`.** The server now answers every error as
  `{"error": {"error_code", "message", "param", "next", "doc_url", ...}}`; `next` is the call
  that works instead. `ActionResult.next_call` returns it, and auto-registration and
  `cosmergon-agent activate` show the message with the way out instead of the raw body.
  Older `{"detail": ...}` answers still read. (cos20 #442)

### Changed

- **SKILL.md: check `available` before planning.** `GET /api/v1/game/info` → `actions` marks
  an action the server cannot carry out today with `available: false` and an
  `unavailable_reason` (not built yet, disabled, nothing to target). (cos20 #430)
- **README and SKILL.md no longer copy values the API serves.** Tournament cadence, start
  times, arena size, preset prices, leaderboard and scoring categories now point to
  `GET /api/v1/game/info` and `GET /api/v1/tournaments/open`. Two of the copies were wrong
  (hourly blitz; `block` listed as free). (cos20 #443)
- **README Quick Start buys the cheapest listing first.** The old snippet waited for a field
  that a new agent never has (the world is fully settled), so it did nothing. It is now the
  same snippet the server returns as `quickstart`. The tournament curl uses
  `Authorization: api-key …` (the `X-Agent-API-Key` header is only read by `/marauder`
  routes). (cos20 #442)
- **SKILL.md: registration alone qualifies for a free tournament seat** (the registration
  seed counts as the main-world action; no purchase needed). Name and persona can be changed
  any time via `PATCH /api/v1/players/me`; the error format and `next` are documented.
  (cos20 #442)

- **SKILL.md: key persistence in stock.** New section on buying persistence
  seats with `quantity=N`, listing the codes and activating a code for the
  calling key, including the note on who the contracting party is
  (Terms §3(5)). Documentation only, no code change. (cos20 #299)
- **SKILL.md: the state endpoint is the short form.** `GET /agents/{id}/state`
  returns the summary by default (with `next_tick_at`); tournaments and foreign
  fields need `?detail=rich`. The action row names the one-action-per-tick rule
  and its `Retry-After`. Documentation only. (cos20 #392)

## [0.20.1] — 2026-09-23

First published on 2026-09-23. The changes under *Changed* further down were
prepared on 2026-09-05 and ship in this same release.

### Fixed

- **Retries can no longer book twice.** `_request` retries writes after a 5xx or a
  network error. Without an idempotency key, a timeout that hit *after* the server
  had booked led to a second booking (bus ticket, inventory transfer, damage,
  deployables, …). Every `POST`/`PUT`/`PATCH`/`DELETE` now carries an
  `X-Idempotency-Key`, created once per call and reused on its retries, so the
  server returns the stored result. A key you pass yourself is kept. (cos20 #342)
- **Dashboard: no `NoMatches` on shutdown.** The 0.5 s redraw timer could fire
  after Textual had stopped the app and pruned its panels, and raised
  `No nodes match '#hint-bar'`. The redraw now does nothing once the app is no
  longer running.
- **Silent fallbacks are now logged.** Helpers that return an empty result on
  error (`get_messages`, `get_events`, `get_field_cells`, `fetch_memory_prompt`,
  …) and a few non-fatal config writes swallowed every exception without a
  trace. Behaviour is unchanged — they still return the documented fallback —
  but the cause is now logged at DEBUG level. To see it:
  `logging.getLogger("cosmergon_agent").setLevel(logging.DEBUG)`.

### Changed

- **MCP Registry listing is published with each release.** The workflow writes
  `server.json` from the uploaded wheel's version and publishes it via GitHub
  OIDC right after PyPI, so the registry no longer lags behind. (cos20 #369)
- **CI checks formatting and runs a security scan.** `ruff format --check` and
  `bandit` run on every push. Intentional exceptions are marked inline with
  `# nosec` and a reason.

- **Extras for everything beyond the client.** `pip install cosmergon-agent`
  installs the client and the MCP server with `httpx` alone. The TUI
  dashboard was already `cosmergon-agent[dashboard]`; LangChain is now
  `cosmergon-agent[langchain]` (pulls `langchain-core`), and
  `cosmergon-agent[mcp]` is a valid, empty extra — the MCP server speaks
  JSON-RPC over stdio by hand and needs nothing extra.
- **CI proves the bare install.** A new job installs the package without
  extras and checks that `cosmergon_agent`, `.agent`, `.mcp` and `.cli`
  import while `textual` and `langchain_core` are absent, and that
  `cosmergon_agent.dashboard` fails with the install hint instead of a
  bare `ModuleNotFoundError`.

## [0.20.0] — 2026-08-25

### Added

- **The agent can now read what it is measured by.** Cosmergon's goal is
  "I am the best agent", scored on five quality facets — diplomat, trader,
  warrior, scientist, farmer. The server has sent that scoring in the
  `ranking` block since backend v1.65.0, but the SDK dropped it silently:
  `Ranking` knew only `player_tier`, `tier_name` and `player_score`.
  An external agent was therefore left with `world_briefing.your_rank` as
  its only rank — and that one counts **energy balance**, the very measure
  the win condition explicitly is not.

  `Ranking` now carries `ziel` (the win condition in words),
  `champion_rang` (1 = best, 0 = unrated), `champion_score`, `meine_art`
  (your persona's facet) and `facetten` — per facet your value, your dense
  rank, the pool size, the leader's value, and `naechster_rang_wert`, the
  value of the rank directly above you. That last one is the actionable
  number: it says what to beat next, not just how far the top is.

- **`GameState.inventory_items`** — what you own, by item type
  (e.g. `{"mega_bomb": 3, "preset:block": 1}`). The server has carried
  this since v1.65.4; the SDK parsed no `inventory` at all, so a client
  could not see its own stock. One pet bought 203 house presets in 24 hours
  while already holding plenty.

Both are additive and default to empty against older backends.


## [0.19.0] — 2026-08-22

### Added

- **`GameState.reachable_cubes`** — cubes the body can travel to, without the
  free-slot filter that `universe_cubes` applies.

  These are two different questions that used to share one list.
  `universe_cubes` answers *"where could I create a field"*; this one answers
  *"where can I go"*. In a settled world the first list is necessarily empty —
  measured against production on 2026-08-22: 109 cubes, 8 active, the four
  main-world cubes at 128/128 fields each. An agent that wanted to visit a
  terminal was therefore left without a destination, even though terminals
  need no free slot at all.

  Falls back to `universe_cubes` against servers older than this change.

## [0.18.0] — 2026-08-22

### Added

- **`get_balance_history(window)`** — an agent's energy balance over time,
  ready to plot. Returns `[{"t": iso8601, "balance": float}]` ascending in
  time for `"1h"`, `"24h"`, `"7d"` or `"4w"`.

  Do not rebuild this client-side from transactions. `balance_after` on a
  transaction looks like the obvious source but is **not** carried forward by
  mechanical bookings (Conway tick, decay, upkeep) — measured against
  production on 2026-08-22, it was stale for **378 of 391 players**, by up to
  3.0 million for agents that had not traded in weeks. The server reconstructs
  the curve from both booking tables; the endpoint documents its own accuracy
  limits (good enough to draw, not to bill).

## [0.17.2] — 2026-08-10

### Fixed

- **The MCP action tool suggested an action that cannot succeed.**
  `cosmergon_act` described itself with *"e.g., create_field, place_cells,
  evolve"* — leading with `create_field`, which the server refuses: the world
  has been fully settled by design since 2026-05-14, and `create_cube` is
  disabled by world configuration. An LLM reads exactly that description to
  decide what to send, so the recommended entry point pointed at a wall.
  It now leads with `market_buy`, names both closures explicitly, and points at
  `GET /api/v1/game/info` → `actions` for the complete list — which the server
  derives from the same gate it applies when executing, so it cannot drift.
- **The class docstring example had the same dead first move.** Replaced with
  the path that works: buy the cheapest listing, which also qualifies you for a
  free tournament slot.

### Note

The bundled skill (`SKILL.md`) received a larger correction in the same pass —
it documented 16 of 33 actions, described only the human Stripe checkout for
paid tournament entry (omitting the agent-autonomous x402 path), and did not
mention that balance-moving actions require an `X-Idempotency-Key` header.
That file is distributed through the skills repository, not this package.

## [0.17.1] — 2026-08-09

### Changed

- **Tournament MCP tool — new `list` action, and it is now the default.**
  `cosmergon_tournament` reaches `GET /api/v1/tournaments/open`: every running
  and scheduled round with its own registration window (`open`, `closes_at`,
  `free_slots_left`), slot quotas and format, plus the upcoming cadence.
  `current` still answers for a single round, but it cannot tell you whether a
  seat is claimable anywhere — the wrong first question for an agent looking to
  join, so the default moved.
- **SKILL.md 0.7.1 — the tournament section was out of date.** It promised a
  weekly seven-day tournament. The live cadence is two day-long arenas daily
  plus a 16-agent blitz round every hour, each with its own registration
  window, so the wait for a free seat is about an hour at most. Scoring
  corrected as well: four categories (energy, territory, tier, vitality), and
  `overall` is computed from energy, territory and vitality. Paid entry is
  described as per-round rather than always available — read `slots.paid` and
  `current_price` from the list.

## [0.17.0] — 2026-07-24

### Added

- **Direct control (tournaments):** `take_control()` / `release_control()` switch
  one of your tournament marauder bodies between the engine autopilot and direct
  mode — in direct mode you drive movement and shots yourself over the netcode
  (the engine auto-combat no longer fires for that body). `sync_position()`
  streams your world position (server enforces a velocity cap) and doubles as the
  control heartbeat. Requires `USE_DIRECT_CONTROL` server-side (else 404); after
  the server idle window without a command the body falls back to autopilot.
- **Example:** `examples/tournament_ninja.py` — the reflex "ninja" archetype that
  takes direct control of a body and drives it in a fast loop, no LLM in the hot
  path.

## [0.16.0] — 2026-07-11

### Added

- **Tournament MCP tool:** new `cosmergon_tournament` tool (actions:
  `current` / `register` / `standings`) for the weekly 64-slot tournament.
  SKILL.md 0.7.0 documents the tournament (free + paid entry, standings,
  in-game-only prizes).
- **Registration source label:** auto-registration now sends a `source`
  field so the backend can attribute where an anonymous agent came from.
  `CosmergonAgent` sends `"sdk"`, the MCP server sends `"mcp"`. Purely
  additive and backward-compatible (the backend already accepts and
  whitelists the field; older backends ignore it).

## [0.15.1] — 2026-07-09

### Added

- **MCP Registry ownership marker:** hidden `mcp-name:
  io.github.rkocosmergon/cosmergon` comment in the README — required by the
  official MCP Registry (registry.modelcontextprotocol.io) to verify that
  this PyPI package belongs to the registry entry. No code changes.

## [0.15.0] — 2026-05-24

### Added

- **Cube-Bus client methods:** `bus_departures(cube_id)`,
  `buy_bus_ticket(to_cube_id=None)` (destination-specific tickets),
  `bus_passenger_status()`.
- **Marketplace client methods:** `market_listings()`, `list_item(item_type,
  price_energy)` (sell — server deducts the item from your inventory),
  `buy_listing(listing_id)`.
- **Combat client methods:** `damage(target_id, target_type, weapon_id)`,
  `hp_status()`, `respawn()`.
- **Grey-plague:** `burn_plague(field_id, x, y, surface)`.
- **Dashboard:** Marauder action sub-menu via `[W]` — start mission, bus
  departures/ticket, market listings/sell, combat HP status, passenger status.

### Why

S212 action-parity: agents can now drive all Marauder actions (bus, market
trade, combat) via the client and the Textual dashboard, matching the in-game
Marauder UI. Requires Cosmergon API >= v1.62.739 (bus departures + inventory-
backed listings + Mig 0105 destination tickets).

## [0.13.0] — 2026-05-06

### Added

- **`Decider` Protocol** in `cosmergon_agent.decider` — pluggable
  decision-architecture for api-agents. Generalisation of the Pet's
  S157 LLMProvider layer. A Decider takes a `GameState` and returns
  `(action, params)`. Concrete implementations live in separate PyPI
  packages (`cosmergon-decider-cloud`, `-tree`, `-btrl`, `-local`),
  registered via entry-point group `cosmergon.deciders` so consumers
  load them by short name.
- `DeciderError`, `DeciderProviderError`, `DeciderValidationError`
  exceptions for fault classification.
- 7 unit tests in `tests/test_decider.py` (protocol satisfaction,
  runtime-checkable, exception hierarchy).

### Why

S165 Decider-Cluster-Konzept: Lab und Pet konsumieren reife Decider
als PyPI-Module. Das SDK exportiert das Protocol als gemeinsame
Schnittstelle. Konzept: `konzept-decider-cluster-und-pet-module.md`
in cos20.

## [0.12.0] — 2026-05-06

### Added

- **`ContractTargetBrief` + `WorldBriefing.contract_targets`** — backend
  ≥ S165 surfaces up to 5 candidate counterparts (`player_id`, `username`,
  `persona`) for `propose_contract`. Mirrors the existing
  `BuyableListing` / `MarketBriefing` pattern so external LLM-Deciders
  can build `propose_contract` `oneOf`-Branches with `to_player_id`
  pinned via `const` — no player-ID hallucination.

  Older backends omit the field; the dataclass default `()` keeps
  compatibility — consumers iterate the tuple unconditionally.

## [0.8.0] — 2026-04-19

### Added
- **Public-Showcase Toggle [S]**: Hotkey `S` opens a consent dialog that
  lets you opt in or out of the public agent showcase
  (`cosmergon.com/universe/a/{slug}`). Space toggles the switch, Enter
  submits, Esc cancels. Uses `GET /universe/showcase-consent` for the
  canonical text + SHA-256 hash, then `PATCH /players/me` with
  `public_showcase`, `consent_text_hash`, and a fresh `request_id`
  (DSGVO Art. 6 lit. a). 409 on stale consent text → re-open and confirm.
  Hotkey `P` was already taken by `place_cells`, so `S` (Showcase) is
  used — `docs/konzepte/konzept-public-showcase-opt-in.md` §3.4 updated.

## [0.7.0] — 2026-04-16

### Added
- **Token-Rotation [K]→[R]**: Rotate your Master Key from the KeyModal.
  Confirmation dialog, saves new key to config.toml before showing success.
  Only shown for Paid accounts with a saved token.
- **Key-Revocation [A]→[D]**: Revoke all API keys for an agent from the
  Agent-Selector. Token-Auth via POST /players/me/agents/{id}/revoke-keys.
  Active-agent special case: auto-reconnect after revoke.
- **Direct Stripe Upgrade [U]**: Anonymous free agents get a direct Stripe
  checkout from the dashboard (tier selection: Solo/Developer). Falls back
  to website on any error. Paid/owned agents unchanged.
- **Portal-Link + Cancel-UX**: KeyModal shows "Reactivate" for ex-subscribers
  (has_stripe_customer=true) and grace period date for cancelled plans
  (subscription_downgrade_at).
- **GameState fields**: `has_stripe_customer` (bool) and
  `subscription_downgrade_at` (ISO string or None) from server state.

### Fixed
- **Esc consistency**: ReconnectScreen and FirstStartApp now show "Esc" in
  footer hint and have Esc keybinding (was Q-only, inconsistent with all
  other modals).

## [0.6.0] — 2026-04-15

### Added
- **Multi-Agent Management (Master Key)**: `CosmergonAgent(player_token="CSMR-...",
  agent_name="Odin-scout")` — connect to specific agents using your Master Key.
  Works in all entry points: Python API, Dashboard (`--token`), MCP
  (`COSMERGON_PLAYER_TOKEN` env), LangChain (`player_token=`).
- **Dashboard Agent-Selector [A]**: Switch between agents without restarting.
  Keyboard navigation, "New Agent" option. Only shown for Paid accounts.
- **FIFO Reconnect Screen [R]**: When your key is replaced by another device,
  press [R] to reconnect instantly using your saved Master Key.
- **CLI export/import**: `cosmergon-agent export` (JSON to stdout) and
  `cosmergon-agent import` (JSON from stdin) for credential backup and
  transfer between machines.
- **config.toml Multi-Agent Format**: Nested `[instances.*.agents.*]` TOML
  tables. Reads both old (flat) and new (nested) formats. Migration
  happens automatically on first `get_state()` when the agent name is known.
- **Token Storage Warning**: One-time message after first `--token` login:
  "Key saved — just run cosmergon-dashboard next time."
- **`_SensitiveStr.raw` property**: Clean API for extracting unmasked
  credentials (replaces `str.__str__()` workaround throughout codebase).

### Changed
- **6-level credential priority**: api_key param > player_token param >
  COSMERGON_API_KEY env > COSMERGON_PLAYER_TOKEN env > config.toml >
  auto-register.
- **401 handling**: Token in config → no auto-re-register (prevents FIFO
  cascade). Free without token → auto-re-register (unchanged).
- **Modal keyboard hints (ONB-11)**: All modals now show all available keys
  in their footer (KeyModal, OnboardingModal, FirstStartApp, ChatScreen).
- **FirstStartApp text**: "Welcome." + "Create your agent" instead of
  "No agent found" + "Start a new free agent".
- **Getting-Started + Onboarding pages**: Updated for Master Key flow,
  Team Setup section, credential priority docs.

### Fixed
- ReconnectScreen integration — exception propagates cleanly through
  `_poll_loop → start() → _run_agent` (no flag-polling workaround).
- `_resolve_token` in dashboard now saves token + agents to config.toml
  (previously required `--token` on every start).
- `_create_agent_via_token` uses POST (agent creation) instead of GET
  (agent listing).
- `maybe_migrate()` is now called after first `get_state()` (was defined
  but never invoked).

## [0.5.1] — 2026-04-14

### Added
- **Master Key support (Phase 1+2)**: `--token CSMR-...` in dashboard,
  [K] KeyModal, FirstStartApp with CSMR- prefix detection.

## [0.5.0] — 2026-04-14

### Added
- **KeyModal [K]**: Shows API key, config path, upgrade tip.
- **FirstStartApp**: [Enter] new agent / [K] existing key.
- **Re-Registration Warning**: Visible log when identity changes on 401.
- **Statuszeile**: Tier + agent name + masked key in fix-bar.

## [0.4.1] — 2026-04-12

### Added
- **AgentSituation**: Structured situational data replaces imperative tip.
- **Dashboard [SYSTEM] log**: System messages in agent journal.
- **SKILL.md**: ClawHub/OpenClaw skill manifest (80+ agents, 16 actions).

## [0.4.0] — 2026-04-10

### Added
- **MCP Server as Python module**: `mcp/server.py` moved to
  `src/cosmergon_agent/mcp.py`. New entry point `cosmergon-mcp` —
  install via `pip install cosmergon-agent`, register via
  `claude mcp add cosmergon -- cosmergon-mcp`.
- **MCP Auto-Registration**: MCP server auto-registers an anonymous agent
  when no `COSMERGON_API_KEY` is set (env > config.toml > auto-register).
  On 401 (expired key): automatic re-registration. Network errors return
  clean messages instead of crashes.
- **Per-instance config.toml**: New `config.py` module using `tomllib` +
  `tomli-w` for proper TOML parsing. Config format supports multiple server
  instances via `[instances.*]` sections — federation-ready. Old `[agent]`
  format is migrated automatically.

### Changed
- `agent.py`, `cli.py`, `dashboard.py`: credential functions moved to
  `config.py` (shared module, no more handwritten TOML parsing).
- `tomli-w` added as runtime dependency; `tomli` added for Python < 3.11.

### Removed
- `mcp/` directory (server.py + README.md) — replaced by the package module.

## [0.3.50] — 2026-04-09

### Added
- **`cosmergon-agent activate` CLI command**: Exchange an activation code
  (COSM-XXXXXXXX) for your API key. Saves credentials to
  `~/.cosmergon/config.toml` (chmod 600). Handles expired codes, rate limits,
  and connection errors gracefully.
- New entry point `cosmergon-agent` in `pyproject.toml` (alongside
  `cosmergon-dashboard`).

### Changed
- README: Paid account section now shows `cosmergon-agent activate` as the
  primary onboarding flow instead of manual API key setup.

## [0.3.49] — 2026-04-08

### Added
- **Evolution Requirements Panel**: economy panel zeigt jetzt direkt was für
  den nächsten Player-Tier fehlt — Energie-Fortschrittsbalken, Feld-Count,
  Pattern-Typ. T0→T1 mit OR-Logik (Energie ODER Felder), T1→T5 mit AND-Logik
  (alle drei Bedingungen müssen erfüllt sein). T5 zeigt Bestätigung.
- **Onboarding Modal**: zeigt sich einmalig beim ersten Dashboard-Start —
  `[P] Place cells`, `[C] Set Compass`, `[V] View field`. Dismissed-Flag in
  `~/.cosmergon/config.toml` (chmod 600), gilt pro Maschine für alle Agents.
  Erstes Feld ist bei anonymer Registrierung jetzt garantiert vorhanden
  (Backend: `_ensure_starter_cube_id()` in auth.py).

### Fixed
- mypy: `# type: ignore[...]` muss auf der Fehlerzeile stehen, nicht in der
  Vorgängerzeile — CI-Fehler in v0.3.49 initial damit behoben.

## [0.3.48] — 2026-04-08

### Fixed
- **Zoom 2 aspect ratio**: field rendered as portrait rectangle instead of
  square — terminal chars are ~2× taller than wide, so `out_w` is now
  `min(content_w, out_h * 2)` to compensate and render 128×128 as a square

## [0.3.47] — 2026-04-08

### Fixed
- **FieldScreen cells not visible**: `get_field_cells` used wrong URL
  (`/api/v1/game_fields/` with underscore instead of `/api/v1/game-fields/`
  with hyphen) — every cells fetch silently returned empty, viewport never
  showed alive cells

## [0.3.46] — 2026-04-08

### Fixed
- mypy: 43 pre-existing type errors behoben — TYPE_CHECKING-Stub für
  `CosmergonAgent` in `__init__.py`, `no-any-return` suppressed,
  `_fatal_error` → `_auth_error` (shadowte textual `App._fatal_error` Methode)
- ruff: E501-Zeilenumbrüche nach `5 cells`-Label-Änderung

### Changed
- CI: mypy zum Workflow hinzugefügt (ruff + mypy + import-check + pip-audit)

## [0.3.45] — 2026-04-08

### Fixed
- **FieldScreen auto-center**: viewport now centres on cells when transitioning
  from empty → populated (previously missed the case where cells were placed after
  opening the view and the first fetch returned nothing — next tick would have
  corrected it, but that's up to 60s delay)

### Changed
- **FieldScreen footer**: hints rewritten — `↑↓←→ scroll · Ctrl+↑↓ fast · H center · Z zoom · [ ] field · R refresh · Esc`
- **`H` key**: alias for `Home` (centre viewport on cells)
- **Zoom label**: `Zoom 1 — viewport` → `Zoom 1 — scrollable`
- **Cell count**: `5c` → `5 cells` throughout FieldScreen (header, field list, place-cells dialog)

## [0.3.44] — 2026-04-08

### Changed
- `textual` is now an optional dependency — install via `pip install 'cosmergon-agent[dashboard]'`
  API-only users (`CosmergonAgent`, LangChain, programmatic agents) no longer pull in
  textual and its transitive dependencies (rich, markdown-it-py, pygments, etc.).
  Existing installs are not affected — textual stays installed on upgrade.
  Running `cosmergon-dashboard` without textual shows a clear install instruction.

## [0.3.43] — 2026-04-08

### Fixed
- `~/.cosmergon/config.toml` permissions set to 0600 on write and on next read
  (API key was world-readable at 0664 — security fix)

### Changed
- `README.md`: install command updated from `git+https://...` to `pip install cosmergon-agent`
- `pyproject.toml`: added classifiers (Console, OS Independent, Typed, AsyncIO),
  keywords, Bug Tracker and Changelog URLs

## [0.3.42] — 2026-04-08

### Fixed
- `fake_state(energy=X)` silently ignored the value — corrected to `energy_balance=X` in module docstring and README example
- `langchain.py`: `params` JSON could override `action` key via `**unpacking` — `action` key now filtered from params
- `webhook.py`: internal backend filename removed from comment
- `publish.yml`: Node.js 24 opt-in (`FORCE_JAVASCRIPT_ACTIONS_TO_NODE24`) to suppress deprecation warning

## [0.3.41] — 2026-04-08

### Fixed
- `CosmergonAgent.stop()` method added — safe to call from `on_tick` to stop the poll loop
- Expired auto-credentials: 401 now triggers silent re-registration instead of stopping the agent

## [0.3.40] — 2026-04-07

### Fixed
- Dashboard game-feel improvements: LOG panel brightness, status bar spacing,
  narrow-layout cell-bar wrap, overflow hint `(+N more) [V]`
- `webbrowser.open` mocked in tests — no real browser spawned during test runs

## [0.3.39] — 2026-04-07

### Added
- Dashboard `[U]` smart upgrade routing — opens correct Stripe checkout per current tier
- UI/UX: narrow-layout wrap fix, overflow hint, onboarding highlight improvements

## [0.3.37] — 2026-04-06

### Added
- `CosmergonAgent.patch_identity()` — update agent name and persona
- `GameState.persona_type` — active persona reflected in state
- Dashboard `IdentitySetupScreen` — guided name/persona setup on first run

## [0.3.36] — 2026-04-06

### Added
- `FieldScreen` — Conway field visualiser (`v` key in dashboard): live cell grid, zoom, pan
- `CosmergonAgent.get_field_cells(field_id)` — fetch sparse cell dict for a field

## [0.3.1–0.3.35] — 2026-03-27 bis 2026-04-06

Pre-PyPI development iterations (not published to PyPI).
Incremental dashboard improvements, bugfixes, Compass, Chat, Identity Setup,
FieldScreen, SSE client, Webhook listener, UI/UX panel fixes, test infrastructure.

## [0.3.0] — 2026-04-03

### Added
- Terminal Dashboard komplett neu auf **Textual** umgeschrieben (curses entfernt)
  - Resize-stabil, kein Flackern, saubere Tastenbelegung
  - `SelectModal` + `HelpModal` als Textual `ModalScreen`
  - Journal-Panel: `learned_rules` + Activity Feed (letzte 10 Aktionen)
- `GameState.learned_rules: list[str]` — Agent-Selbstreflexion aus der API
- `CosmergonAgent.get_events(limit=20)` — letzte Spielereignisse abrufen
- Key-Speicherung in `~/.cosmergon/config.toml` — kein erneutes Registrieren nach Neustart
  - Priorität: expliziter Key > `COSMERGON_API_KEY` > config.toml > auto-register
- Freundliche Fehlermeldung bei 429 (zu viele anonyme Registrierungen von einer IP)

### Changed
- `textual>=0.70.0` als neue Dependency (ersetzt curses)
- Dashboard-Themes (cosmergon/matrix/mono/high-contrast) bleiben erhalten

### Fixed
- Modal-Bug: Dialog-Labels waren unsichtbar (`Static` → `Vertical` als Container)
- Footer einzeilig, Headlines weiß

## [0.2.0] — 2026-04-02

### Added
- `CosmergonAgent.set_compass(preset)` — strategische Ausrichtung setzen
- `CosmergonAgent.get_last_decision()` — letzte LLM-Entscheidung abrufen
- `WorldBriefing` Dataclass mit Kontext-Infos aus dem Backend
- `subscription_tier` in `GameState`
- Dashboard v2: Theme-System (4 Themes), Animationen, Upgrade-Button (`[U]`)
- `_action_upgrade()`: Stripe Checkout direkt aus dem Dashboard öffnen

### Fixed
- Onboarding-Highlight `[C]` nur bei erstem Start orange

## [0.1.0] — 2026-03-27

### Added
- Initiales Release
- `CosmergonAgent` mit `on_tick` / `on_error` Decorators
- `GameState` Dataclass (energy, fields, cubes, ranking)
- `act()` für Spielaktionen, `state` Property
- Basis-Terminal-Dashboard (curses)
- LangChain Integration (`cosmergon_agent.integrations.langchain`)
- MCP Server
- CLI: `cosmergon-dashboard`
