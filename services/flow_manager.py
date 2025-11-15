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
                                # End of branch, move to next entry in main flow
                                return steps[idx + 1] if idx + 1 < len(steps) else None
                        else:
                            # Fallback: use first occurrence
                            pos = names.index(last_step_id) + 1
                            if pos < len(funcs):
                                return funcs[pos]
                            else:
                                # End of branch, move to next entry in main flow
                                return steps[idx + 1] if idx + 1 < len(steps) else None
                elif branch is None or branch not in entry:
                    # No branch specified or branch doesn't exist - check all branches for last_step_id
                    found_in_branch = False
                    for branch_name, funcs in entry.items():
                        names = [f.__name__ for f in funcs]
                        if last_step_id in names:
                            found_in_branch = True
                            if step_index is not None and 0 <= step_index < len(funcs):
                                pos = step_index + 1
                                if pos < len(funcs):
                                    return funcs[pos]
                                else:
                                    # Last step in branch completed - move to next entry in main flow
                                    return steps[idx + 1] if idx + 1 < len(steps) else None
                            else:
                                pos = names.index(last_step_id) + 1
                                if pos < len(funcs):
                                    return funcs[pos]
                                else:
                                    # Last step in branch completed - move to next entry in main flow
                                    return steps[idx + 1] if idx + 1 < len(steps) else None
                    # If last_step_id WAS found in a branch and we returned None above,
                    # it means the branch is complete - don't return the dict again
                    # Only return dict if we're transitioning INTO branching (last_step_id not in any branch)
                    if not found_in_branch and last_step_id is not None:
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
                            # End of list, move to next entry in main flow
                            return steps[idx + 1] if idx + 1 < len(steps) else None
                    else:
                        pos = names.index(last_step_id) + 1
                        if pos < len(entry):
                            return entry[pos]
                        else:
                            # End of list, move to next entry in main flow
                            return steps[idx + 1] if idx + 1 < len(steps) else None
                elif last_step_id is None:
                    return entry
            else:
                if entry.__name__ == last_step_id:
                    return steps[idx+1] if idx+1 < len(steps) else None
        return None
    
    def get_flow(self, action: str) -> Optional[List[Entry]]:
        """Get the complete flow definition for an action."""
        return self.flows.get(action)
    
    def get_last_step_name(self, flow_def: List[Entry]) -> Optional[str]:
        """
        Get the name of the last step in a flow definition.
        Handles branching by returning the last step of the first branch.
        """
        if not flow_def:
            return None
        
        last_entry = flow_def[-1]
        
        # If last entry is a dict (branching), get the first branch's last step
        if isinstance(last_entry, dict):
            # Get first branch
            first_branch_key = next(iter(last_entry.keys()))
            branch_funcs = last_entry[first_branch_key]
            if isinstance(branch_funcs, list) and branch_funcs:
                return branch_funcs[-1].__name__
            return None
        
        # If last entry is a list (parallel steps)
        if isinstance(last_entry, list):
            if last_entry:
                return last_entry[-1].__name__
            return None
        
        # Single callable
        return last_entry.__name__

flow_manager = FlowManager()
