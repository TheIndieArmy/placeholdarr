# services/flow_manager.py
import pkgutil
import importlib
from typing import Dict, List, Callable, Union, Optional
from services.actions import __path__ as actions_path

Entry = Union[Callable, List[Callable], Dict[str, List[Callable]]]

class FlowManager:
    """
    Manages flows defined in services/actions/*_flow.py modules.
    Each module exports a steps() returning a list where items can be:
      - Callable: single step
      - List[Callable]: parallel steps
      - Dict[str, List[Callable]]: branching by key
    """
    def __init__(self):
        self.flows: Dict[str, List[Entry]] = {}
        for _, name, _ in pkgutil.iter_modules(actions_path):
            module = importlib.import_module(f"services.actions.{name}")
            action = name.replace('_flow', '')
            if hasattr(module, 'steps'):
                self.flows[action] = module.steps()

    def get_initial(self, action: str) -> Optional[Entry]:
        """First entry in the flow for action."""
        return self.flows.get(action, [None])[0]

    def get_entry_id(self, action: str, entry: Entry) -> str:
        """Generate ID string for an entry."""
        if isinstance(entry, dict):
            return ','.join(entry.keys())
        if isinstance(entry, list):
            return ','.join(f.__name__ for f in entry)
        return entry.__name__

    def get_entry_by_id(self, action: str, entry_id: Optional[str]) -> Optional[Entry]:
        """Resolve an entry_id string back to the Entry in the flow."""
        steps = self.flows.get(action, [])
        for entry in steps:
            eid = self.get_entry_id(action, entry)
            if eid == entry_id:
                return entry
        return None

    def next_entry(
        self,
        action: str,
        branch: Optional[str],
        last_step_id: Optional[str],
        step_index: Optional[int] = None
    ) -> Optional[Entry]:
        """
        Return the entry following last_step_id (and step_index if provided) within the given branch (if branching) or globally.
        Handles repeated steps by using step_index to disambiguate.
        """
        steps = self.flows.get(action, [])
        if not steps:
            return None
        # If no last, return first
        if last_step_id is None:
            return steps[0]
        # Iterate
        for idx, entry in enumerate(steps):
            if isinstance(entry, dict):
                # branch-specific sequence
                if branch and branch in entry:
                    funcs = entry[branch]
                    names = [f.__name__ for f in funcs]
                    # Use step_index to disambiguate repeated steps
                    if last_step_id in names:
                        # If step_index is provided, use it
                        if step_index is not None and 0 <= step_index < len(funcs):
                            pos = step_index + 1
                            if pos < len(funcs):
                                return funcs[pos]
                            else:
                                # End of branch, return None to terminate
                                return None
                        else:
                            # Fallback: use first occurrence
                            pos = names.index(last_step_id) + 1
                            if pos < len(funcs):
                                return funcs[pos]
                            else:
                                return None
                elif branch is None or branch not in entry:
                    # No branch specified or branch doesn't exist - check all branches for last_step_id
                    for branch_name, funcs in entry.items():
                        names = [f.__name__ for f in funcs]
                        if last_step_id in names:
                            if step_index is not None and 0 <= step_index < len(funcs):
                                pos = step_index + 1
                                if pos < len(funcs):
                                    return funcs[pos]
                                else:
                                    return None
                            else:
                                pos = names.index(last_step_id) + 1
                                if pos < len(funcs):
                                    return funcs[pos]
                                else:
                                    return None
                    # If last_step_id not found in any branch but we're at a dict entry,
                    # it means we're transitioning into branching - return the dict itself
                    if last_step_id is not None:
                        return entry
            elif isinstance(entry, list):
                names = [f.__name__ for f in entry]
                # Use step_index for repeated steps
                if last_step_id in names:
                    if step_index is not None and 0 <= step_index < len(entry):
                        pos = step_index + 1
                        if pos < len(entry):
                            return entry[pos]
                        else:
                            return None
                    else:
                        pos = names.index(last_step_id) + 1
                        if pos < len(entry):
                            return entry[pos]
                        else:
                            return None
                elif last_step_id is None:
                    return entry
            else:
                if entry.__name__ == last_step_id:
                    return steps[idx+1] if idx+1 < len(steps) else None
        return None

flow_manager = FlowManager()
