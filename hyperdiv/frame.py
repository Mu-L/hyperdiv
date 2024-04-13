"""
A 'Frame' serves as a context in which to execute critical stages
in the lifecycle of the application.

- A frame guards access to the app runner and state. Different access
  methods are exposed by different frames. For example, code running
  in a RenderFrame cannot mutate state.

- A frame can gather and store context information. For example, when
  running the user app, AppRunnerFrame collects read dependencies and
  dirty mutations, and manages the dom collector stack and component
  keys.

Hyperdiv code specifies which frame it is requesting, by calling
`frame_cls.current()`. `current()` verifies that the current frame is
an instance of `frame_cls`. This catches errors in code that may
attempt to use the wrong frame type.
"""

import contextvars
from .debug import logger
from .collector import CollectorStack


class Frame:
    """Base class. Should not be directly instantiated."""

    _current_frame: contextvars.ContextVar = contextvars.ContextVar("current_frame")

    def __init__(self, app_runner):
        self._app_runner = app_runner
        self.component_count = 0

    def __enter__(self):
        self._current_frame_token = Frame._current_frame.set(self)

        logger.debug(f"----- Enter {type(self).__name__} -----")
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        logger.debug(f"----- Exit {type(self).__name__} -----")
        Frame._current_frame.reset(self._current_frame_token)

    @classmethod
    def current(cls):
        frame = Frame._current_frame.get(None)
        if not frame:
            raise RuntimeError("There's no active Hyperdiv frame.")
        if not isinstance(frame, cls):
            raise RuntimeError(f"Wrong frame: Expected {cls} but got {type(frame)}")
        return frame


class StateAccessFrame(Frame):
    """
    A frame that gives basic read/write access to the application
    state. Guards against mutations of read-only & event props and
    against resetting event props.
    """

    @property
    def state_lock(self):
        return self._app_runner.state.state_lock

    def get_state(self, key, prop_name):
        return self._app_runner.state._get(key, prop_name)

    def update_state(self, key, prop_name, value):
        prop = self._app_runner.state.get_prop(key, prop_name)

        if prop.is_event_prop or prop.backend_immutable:
            raise ValueError(f"Prop {prop_name} cannot be mutated.")

        return self._app_runner.state._update(key, prop_name, value)

    def reset_state(self, key, prop_name):
        prop = self._app_runner.state.get_prop(key, prop_name)

        if prop.is_event_prop:
            raise ValueError(f"Event prop {prop_name} cannot be reset.")

        return self._app_runner.state._reset(key, prop_name)

    def init_props(self, key, props_with_values):
        self._app_runner.state.init_props(key, props_with_values)

    def has_prop(self, key, prop_name):
        return self._app_runner.state.has_prop(key, prop_name)

    def get_props(self, key):
        return self._app_runner.state.get_props(key)

    def trigger_event(self, key, prop_name, value):
        self._app_runner.trigger_event(
            self._app_runner.state.get_prop(key, prop_name),
            value,
        )


class AppRunnerFrame(StateAccessFrame):
    """
    The user app function is run in the context of this frame. In
    addition to read/write state access, it manages scopes, component
    keys, read dependencies, dirty mutations, commands, etc.
    """

    def __init__(self, app_runner, prev_frame_mutations=None):
        super().__init__(app_runner)

        self.prev_frame_mutations = (
            prev_frame_mutations if prev_frame_mutations else set()
        )

        # The collector stack used in capturing the UI hierarchy/dom
        # while the app function is running.
        self.collector_stack = CollectorStack()
        # The keys generated/used by the app function.
        self.keys = set()
        # The read dependencies generated by the app function. A set
        # of (key, prop_name) tuples.
        self.deps = set()
        # The mutations caused by the app function. A set of (key,
        # prop_name) tuples.
        self.mutations = set()
        # The event mutations that need to be reset at the end of the
        # run.
        self.scope_stack = []

    # Cache access

    def cache_get(self, cache_key):
        return self._app_runner.cache.get(cache_key)

    def cache_put(self, cache_key, value):
        self._app_runner.cache.put(cache_key, value)

    # Running tasks

    def task_frame(self):
        return TaskFrame(self._app_runner)

    def run_task_on_ioloop(self, coro):
        self._app_runner.task_runtime.run_on_ioloop(coro)

    def run_task_in_threadpool(self, fn):
        self._app_runner.task_runtime.run_in_threadpool(fn)

    # Storage access

    def get_storage(self, storage_key):
        return self._app_runner.get_storage(storage_key)

    # Scope management

    def push_scope(self, scope_key):
        self.scope_stack.append(scope_key)

    def pop_scope(self):
        self.scope_stack.pop()

    # Commands

    def add_command(self, command):
        self._app_runner.pending_commands.append(command)

    # Override state access to track reads and writes

    def get_state(self, key, prop_name):
        value = super().get_state(key, prop_name)
        self.deps.add((key, prop_name))
        return value

    def update_state(self, key, prop_name, value):
        updated = super().update_state(key, prop_name, value)
        if updated:
            self.mutations.add((key, prop_name))

    def reset_state(self, key, prop_name):
        updated = super().reset_state(key, prop_name)
        if updated:
            self.mutations.add((key, prop_name))

    # Dirty checking

    def filter_dirty_deps(self, deps):
        mutations = self.mutations.union(self.prev_frame_mutations)
        return mutations.intersection(deps)

    def deps_are_dirty(self, deps):
        return len(self.filter_dirty_deps(deps)) > 0


class TaskFrame(StateAccessFrame):
    """
    Task functions are run in the context of this frame. Tasks can
    read/write state.

    Tasks cannot create UI components. That check is done by the base
    Component class.
    """

    def update_state(self, key, prop_name, value):
        updated = super().update_state(key, prop_name, value)
        if updated:
            # Register the task mutation with the app runner,
            # triggering an app-rerun if the app depends on the
            # mutated prop.
            self._app_runner.enqueue_task_mutations([(key, prop_name)])

    def reset_state(self, key, prop_name):
        updated = super().reset_state(key, prop_name)
        if updated:
            self._app_runner.enqueue_task_mutations([(key, prop_name)])


class UIUpdatesFrame(StateAccessFrame):
    """
    Incoming updates from the browser (and triggered events) are
    applied in this frame. It has basic read/write access and tracks
    mutations. The mutations will be passed to a subsequent
    AppRunnerFrame as its `prev_frame_mutations` argument.
    """

    def __init__(self, app_runner):
        super().__init__(app_runner)
        self.mutations = set()
        self.event_mutations = set()

    def update_state(self, key, prop_name, value):
        try:
            prop = self._app_runner.state.get_prop(key, prop_name)
        except KeyError:
            # This can happen when developing. When saving a python
            # file and the server is restarted, the browser may have a
            # lingering update in the websocket queue, or an event
            # handler may fire right before its element is torn down,
            # corresponding to a component key that no longer exists
            # in the updated code.
            logger.warn(f"Ignoring UI update on nonexistent key {key}.")
            return

        updated = self._app_runner.state._update(key, prop_name, value)
        if updated:
            self.mutations.add((key, prop_name))
            if prop.is_event_prop:
                self.event_mutations.add((key, prop_name))
            self._app_runner.ui_prop_state.set_prop_value(prop)

    def reset_state(self, key, prop_name):
        prop = self._app_runner.state.get_prop(key, prop_name)

        updated = self._app_runner.state._reset(key, prop_name)
        if updated:
            self.mutations.add((key, prop_name))
            if prop.is_event_prop:
                self.event_mutations.add((key, prop_name))
            self._app_runner.ui_prop_state.set_prop_value(prop)

    def trigger_event(self, prop, value):
        raise Exception("Cannot access state.")


class ResetUIEventsFrame(StateAccessFrame):
    """
    Event props will be reset within this frame. Code running in this
    frame is restricted to only event prop resets, and cannot do
    anything else.
    """

    def get_state(self, key, prop_name):
        raise Exception("Cannot access state.")

    def init_props(self, key, props_with_values):
        raise Exception("Cannot access state.")

    def has_prop(self, key, prop_name):
        raise Exception("Cannot access state.")

    def update_state(self, key, prop_name, value):
        raise Exception("Cannot access state.")

    def get_props(self, key):
        raise Exception("Cannot access state.")

    def trigger_event(self, prop, value):
        raise Exception("Cannot access state.")

    def reset_state(self, key, prop_name):
        prop = self._app_runner.state.get_prop(key, prop_name)
        if not prop.is_event_prop:
            raise ValueError(f"Cannot reset non-event prop {prop_name}.")
        self._app_runner.state._reset(key, prop_name)


class RenderFrame(StateAccessFrame):
    """
    Rendering the dom (or diff) is done in this frame. The frame is
    restricted roughly to read-only state access, but it can
    initialize components -- namely singletons, which will be sent to
    the UI.
    """

    def get_state(self, key, prop_name):
        raise Exception("Cannot access state.")

    def update_state(self, key, prop_name, value):
        raise Exception("Cannot access state.")

    def reset_state(self, key, prop_name):
        raise Exception("Cannot access state.")

    def trigger_event(self, prop, value):
        raise Exception("Cannot access state.")

    def prop_changed(self, prop):
        return self._app_runner.ui_prop_state.prop_changed(prop)
