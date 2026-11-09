<!-- MISAKA: pi chord v1.3.0's docs/tutorial.md, with every example in Python. Each example is a region of
tests/test_chord_tutorial.py, which checks that the printed copy equals it; the prose is upstream's, adapted only
where it spoke of TypeScript or JavaScript. -->

# Build a shared todo list with Chord

## 1. What problem does Chord solve?

A todo list has data, operations on that data, and several people looking at it.
You could hand-write object creation, subscription cleanup, messages between
processes, and code replacement. Chord connects these pieces for you.

A **host** is the runtime that owns objects. An **object** combines state with
methods at an address, such as “the family todo list.” A **reference** is a caller's
reference to that object, not the object itself. Several references can reach the
same object. References can also reach another host over a connection.

```text
browser host                         server host
ref + subscribed rows -- link --> Todos view (owner or guest)
                                           |
                                        ref
                                           v
                                   TodoStore: rows + methods
```

We will start inside one host, add observable state, then share it with a
browser. Finally we will add background work, plugins, and hot reload.
**Live state** means subscribers receive new values when data changes, without
asking for it again.

<!-- example:01 -->
```python
host = createHost({"self": "server", "name": "todo-server"})
assert host.name == "todo-server"
await host.close()
```

The assertion passes: the host's name is `todo-server`. Creating a host does
not create a todo object, start a server socket, or persist data. `close()`
shuts down its objects and connections.

> **Tip:** These are executable Python examples, using assertions to show
> results. Shared imports are at the end. Later sections reuse earlier contracts
> and functions, but each section's test starts with fresh hosts. Test-only
> cleanup closes hosts even when an assertion fails; snippets omit that cleanup.

## 2. Your first object

Before callers can use an object, they need to agree on its name and members.
That agreement is a **contract**: a runtime value that also describes its
members' types. A **schema** describes a value's shape. We use `Type`, misaka's
stand-in for TypeBox, to write schemas. `method(args, result)` declares a method
by its argument and result schemas: `method()` takes no arguments and returns
nothing, and `method(None, Type.String())`, used below, takes no arguments and
returns a string.

Start with a label for a named list. `local` means only code inside this host
can reach it. The input `{"list": "family"}` identifies which list we want;
it is not an argument to `label`.

<!-- example:02-contract -->
```python
TodoLabel = local(
    "tutorial.todos",
    {
        "version": 1,
        "input": Type.Object({"list": Type.String()}),
        "members": {"label": method(None, Type.String())},
    },
)
```

A **factory** is a synchronous function that creates the object's implementation:
the methods and state callers will see. `serve` registers a contract and its
factory. Registration alone does not activate this lazy object; the first
`get` does.

A **root** is one short tree of calls, such as the work for a button click.
`host.root` starts it. Its **context**, called `ctx`, carries cancellation,
caller identity, and the code version for nested calls. Pass it to methods you
call from that root.

<!-- example:02-call -->
```python
host = createHost({"self": "server"})
host.serve(TodoLabel, lambda input, *_: {"label": lambda _ctx: f"Todos for {input['list']}"})

async def labelOf(ctx: Ctx) -> str:
    todos = await ctx.get(TodoLabel, {"list": "family"})
    return await todos.label(ctx)

result = await host.root(labelOf)
assert result == "Todos for family"
```

The result is `"Todos for family"`. `await ctx.get` resolves once the object is
live (and, as we will see, has its state). The reference acquired through
`ctx.get` is released when this root ends. We never receive the factory's
returned object directly. A standalone call without `ctx`, such as
`todos.label()`, starts its own root. A method without arguments takes only
the context: `todos.label(ctx)`; its implementation is `lambda ctx: …`. Inside a root or method, pass the
existing `ctx` so nested calls stay in the same call tree.

The factory also receives `principal` (who is asking) and `self` (the
object's declaration API); a Python function cannot ignore extra arguments, so
it takes all three (`lambda input, *_: …`). We did not need them yet.

> **Why:** The reference separates callers from implementations. Later, replacing
> code will not require replacing every caller's reference.

## 3. Give the list live state

A label is not a todo list. We need rows that can change, and a count that always
agrees with them. A **cell** owns an observable JSON value. A **derivation**
computes another observable value from cells or other readable sources.

Extend the contract with `rows`, `openCount`, and editing methods. State
members describe values, not methods. This adds members at contract version 1;
older callers that only need `label` still have everything they need.

`self.state(name, schema, initial)` declares a cell and returns it. The name
`rows` is important: on reload, Chord matches that name to the existing cell and
keeps its value. `self.derive(sources, fn)` declares a derivation; it has no
name, because it holds nothing a reload must keep.

<!-- example:03-contract -->
```python
Todo = Type.Object({"text": Type.String(), "done": Type.Boolean()})
Rows = Type.Array(Todo)
TodoStore = local(
    "tutorial.todos",
    {
        "version": 1,
        "input": Type.Object({"list": Type.String()}),
        "members": {
            "label": method(None, Type.String()),
            "rows": Rows,
            "openCount": Type.Number(),
            "add": method(Type.Object({"text": Type.String()})),
            "finish": method(Type.Object({"index": Type.Integer()})),
            "reset": method(Rows),
        },
    },
)
```

The factory now declares a cell and a derivation. It is wrapped in `todoStore`,
which takes the cell's options (section 9 raises its version); for now
`todoFactory` uses the defaults. Each method receives its arguments and a
context; changing the cell updates its readers and its derived count together.
`add`, `finish` and `reset` declare no result, so they return nothing.

<!-- example:03-factory -->
```python
def todoStore(options: StateOptions | None = None) -> Factory:
    """The store's factory; ``options`` versions its rows (section 9)."""

    def factory(input: Any, _principal: Any, self: Any) -> dict[str, Any]:
        list_ = input["list"]
        rows = self.state("rows", Rows, [], options)
        openCount = self.derive([rows], lambda todos: len([todo for todo in todos if not todo["done"]]))

        def add(args: Any, ctx: Ctx) -> None:
            def push(draft: Any) -> None:
                draft.push({"text": args["text"], "done": False})

            rows.change(ctx, push)

        def finish(args: Any, ctx: Ctx) -> None:
            def markDone(draft: Any) -> None:
                todo = draft[args["index"]]
                if not todo:
                    raise invalid("args", "No such todo")
                todo["done"] = True

            rows.change(ctx, markDone)

        def reset(next: Any, ctx: Ctx) -> None:
            rows.replace(ctx, next, {"diff": True})

        return {
            "label": lambda _ctx: f"Todos for {list_}",
            "rows": rows,
            "openCount": openCount,
            "add": add,
            "finish": finish,
            "reset": reset,
        }

    return factory


todoFactory = todoStore()
```

`change(ctx, fn)` gives `fn` a temporary mutable **draft**. Here `add`
pushes a row and `finish` changes one field. The draft is discarded if the
callback throws. The callback must be synchronous. A callback that mutates the
draft returns `None`; one that returns a value replaces the whole value with it
(so `add` does not return `draft.push(…)`, which returns the new length).

`replace(ctx, value)` replaces the whole value. We use it for resetting a
list we own completely. `{"diff": True}` asks Chord to publish just the
structural changes. Without that option, replacement diffs automatically when
the new value shares containers at the same keys with the old one; unrelated
values normally publish a whole-value replacement. Either way, subscribers
receive the same resulting value.

<!-- example:03-call -->
```python
host = createHost({"self": "server", "dev": True})
host.serve(TodoStore, todoFactory)
todos = await host.get(TodoStore, {"list": "family"})
counts: list[int] = []
stop = todos.openCount.subscribe(counts.append)

async def edit(ctx: Ctx) -> None:
    await todos.add({"text": "Buy milk"}, ctx)
    await todos.finish({"index": 0}, ctx)
    await todos.reset([], ctx)

await host.root(edit)
assert counts == [0, 1, 0]
assert todos.rows.value == []
stop()
todos.release()
```

The count subscriber sees `0, 1, 0`: initially empty, one unfinished todo,
then no unfinished todos. Resetting to empty does not add another count
notification because the derived count is still zero. The final rows are `[]`.
`subscribe` calls back immediately with the current value and returns an
unsubscribe function. The awaited `get` makes sure there is a value: a reference
you hold is live, with its state.

> **Why:** Store reloadable state in cells, not closure variables. Treat
> `cell.value`, `ref.rows.value`, and subscriber values as immutable.
> Only mutate drafts inside `change`; do not keep a draft for later use.
> A derivation runs synchronously, so use it for projections, not database work.

## 4. Let objects use objects and resources

Saving a todo may require both the list and a database connection. A
**reference** keeps the object it refers to alive. `self.get` declares a
reference owned by this object; it lasts longer than a single method call.

A **resource** is something the runtime opens and later closes, such as a
database connection. Its `open` may be asynchronous even though the factory
must be synchronous. The example database is deliberately fake: it just saves
strings in memory.

`opaque("name@version")` describes a native, host-only member. It is not
JSON state and cannot be sent over a link. `self.expose` connects that member
to a declared resource, without reading the resource during the factory.

<!-- example:04-database -->
```python
Database = local(
    "tutorial.database",
    {
        "version": 1,
        "input": Type.Object({}),
        "members": {"db": opaque("tutorial.database@1")},
    },
)
SavedTodos = local(
    "tutorial.saved-todos",
    {
        "version": 1,
        "input": Type.Object({"list": Type.String()}),
        "members": {"add": method(Type.Object({"text": Type.String()}))},
    },
)
host = createHost({"self": "server"})
opens = 0
closed = asyncio.Event()

def databaseFactory(_input: Any, _principal: Any, self: Any) -> dict[str, Any]:
    async def open(_options: Any) -> dict[str, list[str]]:
        nonlocal opens
        opens += 1
        return {"saved": []}

    db = self.resource("db", {"version": 1, "open": open, "close": lambda _db: closed.set()})
    return {"db": self.expose(db)}

host.serve(Database, databaseFactory)
```

The factory returns an exposed marker, not an opened connection. Chord runs
`open` after declarations finish. The event named `closed` lets the test
observe eventual cleanup.

Now declare `SavedTodos` as an object that holds both the store and the database.
Its editing method uses these dependencies only after activation.

<!-- example:04-saving -->
```python
host.serve(TodoStore, todoFactory)

def savedTodos(input: Any, _principal: Any, self: Any) -> dict[str, Any]:
    database = self.get(Database, {})
    todos = self.get(TodoStore, {"list": input["list"]})

    async def add(args: Any, ctx: Ctx) -> None:
        database.db["saved"].append(args["text"])
        await todos.add({"text": args["text"]}, ctx)

    return {"add": add}

host.serve(SavedTodos, savedTodos, {"linger": 0})
```

An outside consumer holds a `SavedTodos` reference. We also hold `Database`
directly so we can inspect what was saved.

<!-- example:04-call -->
```python
saved = await host.get(SavedTodos, {"list": "family"})
database = await host.get(Database, {})
await host.root(lambda ctx: saved.add({"text": "Buy milk"}, ctx))
assert database.db["saved"] == ["Buy milk"]
assert opens == 1
statuses: list[str] = []
saved.onStatus(lambda status, _error: statuses.append(status))
database.release()
saved.release()
await closed.wait()
assert saved.status == "released"
assert "released" in statuses
```

After the method, the fake database contains `["Buy milk"]`. It opened once,
even though two objects referenced it. `SavedTodos` and `Database` become live
only after their resources and declared dependencies are ready. That is why
`database.db` is safe inside this live object's method: activation has
already established that dependency. An outside caller awaits its `get`.

When both external references are released, `SavedTodos` is disposed and
releases its own database reference. With no references left, the database closes; we
wait for `closed.wait()` to prove it. The released reference stays
`released`, and `onStatus` reports that transition. `onStatus` watches
changes; unlike state subscription, it does not deliver an initial status.

`linger` is a delay in milliseconds after the last reference is released. Zero disposes
without a grace period; a positive value can avoid reopening an expensive
resource when a screen is briefly hidden. Linger is not persistence.

> **Tip:** A factory only declares dependencies. Never call a referenced object's method or
> read `resource.value` there, including during reload. Activation happens
> afterward. Bump a resource's `version` when its configuration should be reopened.
> Disposal stops tasks and listeners, closes resources, then releases references.

## 5. Who is asking? One store, two views

Owners may add todos; guests may only read them. A **principal** is the
application-defined identity of a caller. A **variant** is a named set of
members a remote object offers to that principal. It lets the UI discover
whether an editing method is available.

Do not create a separate data cell for each viewer: their lists would diverge.
Instead, each view holds the same host-local `TodoStore` and exposes its rows.
`self.get` acts as the host's own principal, so owner and guest views share
that store. Remote view objects themselves are separate per principal key.
A remote contract also says where its objects live: `"host": hostNamed("server")`
puts every `Todos` object on the host named `server`. Section 6 uses that.

<!-- example:05-views -->
```python
Todos = remote(
    "tutorial.todo-view",
    {
        "version": 1,
        "input": Type.Object({"list": Type.String()}),
        "host": hostNamed("server"),
        "variants": {
            "viewer": {"state": {"rows": Rows}},
            "editor": {
                "state": {"rows": Rows},
                "methods": {"add": method(Type.Object({"text": Type.String()}))},
            },
        },
    },
)

# Who may ask: the owner, a guest, or the server's own code.
type Principal = Literal["owner", "guest", "server"]


def serveViews(api: Host) -> None:
    def factory(input: Any, principal: Principal, self: Any) -> dict[str, Any]:
        list_ = input["list"]
        if list_ != "family":
            raise forbidden("This list is not shared")
        store = self.get(TodoStore, {"list": list_})
        if principal == "guest":
            return {"variant": "viewer", "rows": store.rows}

        def add(args: Any, ctx: Ctx) -> Any:
            if len(args["text"].strip()) == 0:
                raise invalid("args", "A todo needs text")
            return store.add({"text": args["text"]}, ctx)

        return {"variant": "editor", "rows": store.rows, "add": add}

    api.serve(Todos, factory)
```

Here principals are the strings `"owner"` and `"guest"`, assigned by
trusted application code, and `"server"`, the host's own code: every host is
created with `self`, the principal its own roots and objects act as. Chord
invents no principals. The `Principal` alias names that type for the
factory's `principal`; Python does not check the comparisons at run time, so
keep them plain. `principalKey` maps identity to object identity. Real
applications should use stable identity, connection provenance, and grant
revision, not display names.

<!-- example:05-call -->
```python
host = createHost({"name": "server", "self": "server", "principalKey": lambda principal: principal})
host.serve(TodoStore, todoFactory)
serveViews(host)
owner = await host.get(Todos, {"list": "family"}, {"principal": "owner"})
guest = await host.get(Todos, {"list": "family"}, {"principal": "guest"})
assert owner.variant == "editor"  # the variant that has add
assert guest.variant == "viewer"
await host.root(lambda ctx: owner.add({"text": "Buy milk"}, ctx), {"principal": "owner"})
assert guest.rows.value == [{"text": "Buy milk", "done": False}]
# A viewer has no add: the provider refuses it.
with pytest.raises(ChordError) as rejected:
    await guest.add({"text": "Not allowed"})
assert isChordError(rejected.value, "not_in_variant")
with pytest.raises(ChordError) as rejected:
    await owner.add({"text": " "})
assert isChordError(rejected.value, "invalid")
```

The owner gets `editor`; the guest gets `viewer`. Both see the same milk
todo. A reference offers the members of its current variant: check `variant`
before using a member only one variant has (`assert owner.variant == "editor"`
does that). The guest's `add`, called anyway, rejects with
`not_in_variant`. Whitespace-only text
rejects with `invalid`: it is a string, as the schema demands, but it breaks a
rule only the method knows. Use `invalid` for bad input and `forbidden` only
for callers who may not do something.

> **Why:** A variant is not all of authorization. The factory checks which
> list may be opened; the method checks the operation's arguments. Production
> code must also enforce grants that can change. Use
> `host.invalidatePrincipal(match)` to revoke matching objects; held references
> become `gone`. A UI watches `onStatus` and gets a new reference when appropriate.
> Guest-provided names or message text must never establish identity.

## 6. Reach the list from another process

A browser cannot call a host-only contract. `remote` contracts describe only
schema-checked state and methods that can travel as JSON. Our `Todos` view is
remote; the underlying `TodoStore` and database stay local. A caller never
names a host: `browser.get(Todos, …)` goes to `server` because the contract
says so, and the server refuses to serve, or to be asked for, a type it is not
the place of. A type that lives on one host per session says
`"host": hostFamily("session:", "session")`: its input's `session` field picks
the host (`{"session": "S1"}` lives on `session:S1`).

A **link** carries ordered messages between hosts. A memory link runs both ends
in one test process, but uses the remote protocol. The accepting host stamps
the caller's principal from its authenticator, not from method arguments or a
client's claimed identity. The connecting side names the principal its parent's
own requests act as (`"upstream": {"link": link, "principal": principal}`, or
`connect(link, {"principal": principal})`).

<!-- example:06 -->
```python
server = createHost({"self": "server", "name": "server"})
server.serve(TodoStore, todoFactory)
serveViews(server)
down, up = memoryLinkPair()
server.accept(down, {"principal": "owner", "kind": "client"})
browser = createHost(
    {
        "self": "browser",
        "name": "browser",
        "kind": "client",
        "upstream": {"link": up, "principal": "server"},
    }
)
todos = await browser.get(Todos, {"list": "family"})
assert todos.variant == "editor"
assert todos.rows.value == []
updated = asyncio.Event()

def onRows(rows: Any) -> None:
    if len(rows) == 1:
        updated.set()

stop = todos.rows.subscribe(onRows)
await browser.root(lambda ctx: todos.add({"text": "Buy milk"}, ctx))
await updated.wait()
assert todos.rows.value == [{"text": "Buy milk", "done": False}]
# Deliberately break the schema to simulate a malformed caller.
with pytest.raises(ChordError) as rejected:
    await todos.add({"text": 123})
assert isChordError(rejected.value, "invalid")
stop()
# A held reference keeps its state without listeners.
assert todos.rows.value == [{"text": "Buy milk", "done": False}]
todos.release()
```

The browser sees an empty snapshot, then the new milk todo. `ready` means
the remote reference has bound to a live object **and** has its state: every
state member of its variant has a snapshot. A held reference stays subscribed,
with or without listeners, until it is released.

What crosses the wire: the input and consumer's contract description, the
provider's shape (variant and members), method arguments and results, and the
state of the reference's variant. State starts with a **snapshot**, the whole current value;
subsequent **deltas** describe changes. Rebinds and reloads can send a
`reset`, a fresh whole value. Functions, resource connections, and cell
instances do not cross the wire.

The deliberate bad argument rejects with `invalid`. Remote arguments,
results, and outgoing state are validated at the provider boundary. Local
methods do not get per-call schema validation; they still need app-level checks.
`"dev": True` additionally checks cell writes while developing.

> **Tip:** Replace `memoryLinkPair` with `webSocketLink` for a WebSocket or
> `ipcLink` for a child-process IPC channel; object contracts stay the same.
> Those adapters wrap an existing connection, not an authenticator. Configure
> `accept` with an authenticated principal. A browser is `"kind": "client"`;
> a trusted child host is accepted with `"kind": "host"` and its configured name.

## 7. React to changes and run background work

Not all work comes from method calls. The app may update an activity indicator
when rows change, or run a background reminder. A **listener** reacts to an
observable source. A **task** is background execution owned by an object,
which is aborted when the object is disposed or its code is reloaded.

Our activity object mirrors the row count. Its task inserts a sample todo,
then waits for shutdown. The `asyncio.Event`s make those events deterministic
in a test; they are not stored application state.

<!-- example:07 -->
```python
Activity = local(
    "tutorial.activity",
    {
        "version": 1,
        "input": Type.Object({}),
        "members": {"seen": Type.Number()},
    },
)
host = createHost({"self": "server"})
host.serve(TodoStore, todoFactory)
observed = asyncio.Event()
seeded = asyncio.Event()
stopped = asyncio.Event()

def activityFactory(_input: Any, _principal: Any, self: Any) -> dict[str, Any]:
    todos = self.get(TodoStore, {"list": "family"})
    seen = self.state("seen", Type.Number(), 0)

    def onRows(rows: Any, ctx: Ctx) -> None:
        seen.replace(ctx, len(rows) if isinstance(rows, list) else 0)
        if isinstance(rows, list) and len(rows) == 1:
            observed.set()

    self.on("rows", todos.rows, onRows)

    async def task(ctx: Ctx) -> None:
        await todos.add({"text": "Review the list"}, ctx)
        seeded.set()
        aborted = asyncio.Event()
        if ctx.signal.aborted:
            aborted.set()
        else:
            ctx.signal.addEventListener("abort", aborted.set, once=True)
        await aborted.wait()
        stopped.set()

    self.task(task)
    return {"seen": seen}

host.serve(Activity, activityFactory)
activity = await host.get(Activity, {})
await asyncio.gather(seeded.wait(), observed.wait())
assert activity.seen.value == 1
activity.release()
await stopped.wait()
```

The final `seen` is `1`. Each listener event receives a fresh root's
`ctx`; its write belongs to that short call tree. A task is not one enormous
root: Chord method calls made with its task context start fresh short roots.
We pass that context into `todos.add` so cancellation follows the task.
Releasing the activity reference aborts its task and resolves `stopped`.

`self.on(name, source, handler)` declares a listener; `self.task(fn)` a task.
A listener on state, like `rows` here, gets the current value when it starts,
then every change. It gets the current value again whenever a reload replaced
its source (a derivation is new at every reload), so its handler must
reconcile (`seen` is set from the rows, not counted up). Listeners can also
subscribe to a resource's event stream with `self.on(name, resource, select,
handler)`: `select` gets the opened resource and returns something with
`subscribe`; an event stream delivers no current value. Kept subscriptions
survive reload while the resource and selector remain unchanged; the handler
switches to new code. A selector that captures changing configuration should
get a new listener name. Tasks have no names: a reload restarts them all.

> **Why:** Do not wait for a person, sleep for minutes, or run an endless loop
> inside a method. That pins a root and can block state migration. Store a
> pending request in a cell and answer in another call, or use a task that obeys
> `ctx.signal`. Never retain a completed method's `ctx` for background work.

## 8. Package features as plugins

Now a plugin can contribute a menu action without editing a central switch.
A **registry** is a named table of contributions. A **facet** is a plugin's code
for one kind of host: it serves object types. A plugin can have server, worker, and browser
facets, each installing that part of the feature.

Registrations belong to objects of **lifetime `"host"`**: the host creates such
an object when its facet loads and keeps it until the host closes, even if no
screen asks for it. That keeps our menu contribution present. Objects of the
default lifetime, `"referenced"`, are still created only when referenced.

<!-- example:08 -->
```python
Actions = defineRegistry("tutorial.actions")
Menu = local("tutorial.menu", {"version": 1, "input": Type.Object({})})
click: Callable[[], Awaitable[int]] | None = None
storeFacet = Facet(
    name="todo-store",
    **vars(defineFacet(lambda api: api.serve(TodoStore, todoFactory))),
)

def menuSetup(api: FacetApi) -> None:
    nonlocal click

    def menu(_input: Any, _principal: Any, self: Any) -> dict[str, Any]:
        todos = self.get(TodoStore, {"list": "family"})

        async def addSample(ctx: Ctx) -> int:
            await todos.add({"text": "Try plugins"}, ctx)
            return todos.openCount.value

        self.register(Actions, {"addSample": addSample})
        return {}

    api.serve(Menu, menu, {"lifetime": "host"})

    def clickAddSample() -> Awaitable[int]:
        return api.root(lambda ctx: api.registry(Actions).get("addSample", ctx)(ctx))

    click = clickAddSample

menuFacet = Facet(
    name="todo-menu",
    **vars(defineFacet(menuSetup, {"uses": [TodoStore]})),
)
host = createHost({"self": "server"})
await host.load([menuFacet, storeFacet], {"awaitHostObjects": False})
await host.ready()
assert click is not None
assert await click() == 1
assert len(host.registry(Actions).table()) == 1
```

Clicking returns `1`, and the registry has one entry. The menu's dependency
can come from a later facet in the load batch: `load` installs the whole
batch before waiting for host objects to activate. The facet declares `uses` so
reload can also check the contract it consumes.

The facet API supplies `serve`, `name`, `root`, and `registry`.
Our ordinary UI callback uses its saved `api.root`, not a process-global
host. Looking up a registry entry with `ctx` uses the table belonging to that
root's code version. Without `ctx`, lookup uses the current table. Duplicate
keys fail by default; `subscribe` can bridge tables to an imperative consumer.

Normally use `await host.load(facets)` (or `await host.install(facet)`),
which waits for those host objects. This example explicitly defers that barrier
with `"awaitHostObjects": False`, then awaits `host.ready()`. That waits for a
snapshot of the host's objects, not for task completion or later loads.
Failures reject the readiness future; abandoning startup requires closing the
host, because installation is not a rollback transaction.

> **Tip:** Use deferred readiness only when needed. A worker may have to await
> an independent local bootstrap reference, announce control readiness so its
> parent admits its link, and only then await `host.ready()`. Waiting for
> upstream-dependent host objects before admission would deadlock.

For real plugin files, set the module's `default` to `defineFacet(...)`. Hosts
configure `createHost({"loader": nodeLoader})`, with `nodeLoader` imported from
`misaka.chord.node`, then pass module paths or file URLs to `load` and `reload`.
The in-memory facet objects here avoid filesystem setup (`Facet(name=…,
**vars(defineFacet(…)))` names a definition, as `{ name, ...defineFacet(…) }`
does upstream). Built-in facets are trusted in-process code, not a sandbox; the
current loader keeps old module copies in memory, so repeated loading has that
limitation.

## 9. Change running code without losing the list

A reference is useful precisely because callers keep it while implementations
change. An **code version** identifies one committed set of code and registry tables.
A root keeps the code version it started with. New roots use the latest one.

### Compatible code change: no waiting

Give the same named facet a new factory. It changes the label but keeps the
cell declaration. This test deliberately pauses one caller to show what an
old root sees; application methods should not pause like this.

<!-- example:09-compatible -->
```python
host = createHost({"self": "server"})

def facet(label: str) -> Facet:
    def factory(input: Any, principal: Any, self: Any) -> dict[str, Any]:
        return {**todoFactory(input, principal, self), "label": lambda _ctx: label}

    return Facet(name="todos", setup=lambda api: api.serve(TodoStore, factory))

await host.load([facet("Original")])
todos = await host.get(TodoStore, {"list": "family"})
await todos.add({"text": "Buy milk"})
entered = asyncio.Event()
resume = asyncio.Event()

async def oldCaller(ctx: Ctx) -> list[str]:
    first = await todos.label(ctx)
    entered.set()
    await resume.wait()  # Test-only pause: pin an old caller during reload.
    return [first, await todos.label(ctx)]

oldRoot = host.root(oldCaller)
await entered.wait()
compatible = await host.reload([facet("Updated")])
assert compatible["gated"] == []
assert await todos.label() == "Updated"
resume.set()
assert await oldRoot == ["Original", "Original"]
assert todos.rows.value[0]["text"] == "Buy milk"
```

Reload commits while the old root is paused: `gated` is empty. New calls see
`"Updated"`; the resumed old root sees `"Original"` twice. The milk row
survives. Compatible code changes do not wait for that old root to finish.

### State change: bump its version and migrate

Suppose a new policy converts existing todo text to uppercase. The schema is
unchanged, but the data's meaning changes. We still bump the cell's **state
version** (`self.state`'s `version` option, default 1). It belongs to one cell,
independently of the contract version. A **migration** (`migrate`) converts an
old value of that cell to the new representation.

<!-- example:09-migrate -->
```python
events: list[str] = []
stopEvents = todos.rows.events(lambda event: events.append(event["type"]))

def migrate(old: Any, from_: int) -> Any:
    assert from_ == 1
    return [{**todo, "text": todo["text"].upper()} for todo in old]

migrated = await host.reload(
    [
        Facet(
            name="todos",
            setup=lambda api: api.serve(TodoStore, todoStore({"version": 2, "migrate": migrate})),
        )
    ]
)
assert len(migrated["gated"]) == 1
assert todos.rows.value == [{"text": "BUY MILK", "done": False}]
assert events == ["snap", "reset"]
stopEvents()
```

The migration receives the old rows and their state version, `1`. It returns
new rows; the reference now reads `[{"text": "BUY MILK", "done": False}]`.
Subscribers get a `reset` event containing the migrated value. The event
assertion sees an initial `snap`, then that `reset`.
One object is gated. A **gate** waits for calls already inside that object to
finish before migrating a private copy, validating it, and committing. Old
roots that had not entered it cannot later use old code over migrated data:
they get `upgrade_conflict` and should retry in a new root.

Changing a cell's schema also requires a version bump, and a migration unless
the old value already fits the new schema. A change of meaning with the same schema,
like this example, cannot be detected automatically. Real migrations must
reference each stored version they support, not assume every installation is
exactly one version behind.

### Contract change: keep an adapter for old callers

Rename `label` to `heading`. That is a breaking contract change, so use
contract version 2. The provider's `accepts[1]` adapter builds the implementation
that version-1 consumers still expect. (A contract's definition is `def_`, as
`def` is a Python keyword.)

<!-- example:09-contract -->
```python
# Remove label from the new provider's contract: it is now called heading.
members = {name: member for name, member in TodoStore.def_["members"].items() if name != "label"}
RenamedStore = local(
    "tutorial.todos",
    {
        "version": 2,
        "input": TodoStore.def_["input"],
        "members": {**members, "heading": method(None, Type.String())},
    },
)

def renamed(input: Any, principal: Any, self: Any) -> dict[str, Any]:
    implementation = todoStore({"version": 2})(input, principal, self)
    label = implementation.pop("label")
    return {**implementation, "heading": label}

await host.reload(
    [
        Facet(
            name="todos",
            setup=lambda api: api.serve(
                RenamedStore,
                renamed,
                {"accepts": {1: lambda current: {**current, "label": current["heading"]}}},
            ),
        )
    ]
)
assert await todos.label() == "Todos for family"
newer = await host.get(RenamedStore, {"list": "family"})
assert await newer.heading() == "Todos for family"
assert todos.rows.value[0]["text"] == "BUY MILK"
```

The old reference's `label` and the new reference's `heading` both return
`"Todos for family"`. Both still reach the migrated rows. The adapter maps
implementations, not references; forwarding the existing method preserves its context.
We keep the cell at version 2 here so this code-only change does not repeat
migration.

> **Why:** Contract versions protect callers; state versions protect stored
> data; resource versions control reopening. Do not use one number for all
> three. Additive compatible contracts can keep their version; breaking shape
> or meaning changes need a contract bump and adapters for old consumers.
> Reload prepares candidates first; preparation failure leaves the old code
> running. A migration gate that misses its deadline aborts rather than
> switching data underneath active old code.

## 10. Test the objects, not a whole deployment

Most implementation needs only a bare host. Use real factories for state tests, and a
fake facet when you want to test delegation without a real database or service.
Here the fake records what the editing view sends to the store.

<!-- example:10 -->
```python
host = createHost({"name": "server", "self": "server", "dev": True})
received: list[str] = []

def fakeSetup(api: FacetApi) -> None:
    def factory(input: Any, principal: Any, self: Any) -> dict[str, Any]:
        return {
            **todoFactory(input, principal, self),
            "add": lambda args, _ctx: received.append(args["text"]),
        }

    api.serve(TodoStore, factory)

fake = Facet(name="fake-store", **vars(defineFacet(fakeSetup)))
await host.load([fake])
serveViews(host)
owner = await host.get(Todos, {"list": "family"}, {"principal": "owner"})
assert owner.variant == "editor"
await owner.add({"text": "Test delegation"})
assert received == ["Test delegation"]
with pytest.raises(ChordError) as rejected:
    await owner.add({"text": ""})
assert isChordError(rejected.value, "invalid")
owner.release()
with pytest.raises(ChordError) as rejected:
    await owner.add({"text": "Too late"})
assert isChordError(rejected.value, "released")
```

The fake received `["Test delegation"]`. Empty text failed before reaching
the fake. The released reference rejects further calls with `released`.
`isChordError(error, code)` checks a stable error code; avoid testing only a
message's wording.

Our other tests exercise the real cells (section 3), resource cleanup (4),
principal variants (5), a client over a memory link (6), and callers kept through
reload (9). After an awaited `get` the state is there; for later changes, await
the value you need. Always unsubscribe and close hosts; leaked references can hide
lifetime bugs or leave background tasks running.

> **Tip:** A fake must honor the contract it serves. This fake delegates all
> ordinary members to `todoFactory` and replaces only `add`; it deliberately
> objects calls rather than updating rows. Do not confuse that with persistence.

## 11. Keep these habits

A temporary call reference should not accidentally become a permanent one. A
screen should not lose its data merely because a button click finishes. Use
`ctx.get` for the first lifetime and `host.get` for the second.

<!-- example:11 -->
```python
host = createHost({"self": "server"})
host.serve(TodoStore, todoFactory)
screen = await host.get(TodoStore, {"list": "family"})

async def click(ctx: Ctx) -> None:
    temporary = await ctx.get(TodoStore, {"list": "family"})
    await temporary.add({"text": "Keep roots short"}, ctx)

await host.root(click)  # The temporary reference is released here; screen still holds the store.
assert screen.openCount.value == 1
screen.release()  # No reference remains; without persistence, disposal loses these rows.
```

The screen still sees count `1` after the root finishes, because it owns a
separate reference. When the screen releases it, no reference remains. This tutorial's cells
are in memory: reload preserves a live object, but disposal or process restart
loses its rows. Real persistence belongs in a database resource, with an
explicit load/save policy.

### Checklist and common mistakes

- Keep reloadable state in cells, not closure variables: factories rerun.
- Change drafts, not published values: mutation outside cells bypasses updates.
- Use `change` for field edits and `replace` for owned snapshots: replacing
  can erase fields your code does not know about.
- Declare dependencies in a synchronous factory, not during methods: activation
  and reload need a complete declaration list.
- Use `ctx.get` for temporary method dependencies: their references end with the call.
- Pass `ctx` into nested calls: cancellation and code versions must stay together.
- Keep roots short and tasks cancellable: migration and shutdown need work to settle.
- Check grants and arguments, not just variants: available methods are not a full policy.
- Stamp principals at trusted ingress, not from client payloads: names are not identities.
- Await `get` before reading state: a reference you hold is live and hydrated, a
  loading one (`host.hold`) throws `not_ready`.
- Release references and unsubscribe: otherwise unused objects remain referenced or observed.
- Bump state versions, contract versions, and resource versions for their own changes:
  each protects a different boundary.
- Await host object readiness before reporting startup success: declared is not yet live.
- Keep reference dependencies acyclic: an object cannot wait for itself through another object.

### Imports and running the examples

These are the shared imports from `tests/test_chord_tutorial.py`;
applications import the same names from `misaka.chord` (and `Type`, misaka's
stand-in for TypeBox, from `misaka.chord._typebox`). `assert` expresses expected
values; `pytest` runs the tests and checks expected errors; `re` and `Path` are
used by the tutorial's source-matching check, not by the application.

<!-- example:imports -->
```python
import asyncio
import re
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Literal

import pytest

from misaka.chord import (
    ChordError,
    Ctx,
    Facet,
    FacetApi,
    Factory,
    Host,
    StateOptions,
    createHost,
    defineFacet,
    defineRegistry,
    forbidden,
    hostNamed,
    invalid,
    isChordError,
    local,
    memoryLinkPair,
    method,
    opaque,
    remote,
)
from misaka.chord._typebox import Type
```

From the repository root, run:

```sh
.venv/bin/python -m pytest tests/test_chord_tutorial.py
```

There is one test per numbered section. Every Python block is matched
against its runnable source, so changing either copy without the other fails a
test. The test harness adds teardown and drift checks; it never uses a real
database, network service, or provider API.

Next read [the usage guide](guide.md) for the API reference and
[the specification](spec.md) for compatibility, lifetime, and reload
rules, including its known limitations (both are upstream's, with TypeScript
examples). If a reference disagrees with `misaka/chord`, the current
implementation wins.
