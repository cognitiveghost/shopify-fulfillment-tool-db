"""Groups Manager for Shopify Fulfillment Tool.

This module provides management of client groups for enhanced profile organization.
Groups allow categorizing clients into custom collections for easier navigation.

Key Features:
    - CRUD operations for custom groups
    - Special groups: "pinned" and "all" (immutable)
    - File locking for safe concurrent access
    - Automatic backups before destructive operations
    - UUID-based group identification
"""

import json
import logging
import uuid
from datetime import datetime
from typing import Any

from shared.store import Store, StoreError, StoreNotFound, StoreUnreachable

logger = logging.getLogger("ShopifyToolLogger")

GROUPS = "Clients/groups.json"
GROUPS_CORRUPTED = "Clients/groups.corrupted.bak"
BACKUPS = "Clients/backups"


# Custom Exception
def _corrupt(e: StoreError) -> bool:
    """Invalid JSON on the server, not a cut-off response (StoreUnreachable, retry later)."""
    return isinstance(e.__cause__, json.JSONDecodeError) and not isinstance(e, StoreUnreachable)


class GroupsManagerError(Exception):
    """Base exception for GroupsManager errors."""


class GroupsManager:
    """Manages client groups configuration.

    This class handles:
    - Loading and saving groups configuration
    - CRUD operations for custom groups
    - File locking for concurrent write protection
    - Automatic backup creation
    - Coordination with ProfileManager for client unassignment

    Attributes:
        store (Store): Server storage; groups live at Clients/groups.json
    """

    def __init__(self, store: Store):
        """Initialize GroupsManager on the server store.

        Args:
            store: Store rooted at the fulfillment directory
        """
        self.store = store

        # Ensure Clients directory exists. Best-effort: MainWindow can now
        # construct this against an unreachable share (ProfileManager's
        # require_connection=False, Bundle 4), and a raise here would
        # re-introduce the quit-on-launch bug that keyword exists to remove.
        # load_groups() already tolerates a missing groups_path -- it is the
        # mkdir itself that has no guard.
        try:
            self.store.claim_dir("Clients")  # False when it already exists
        except OSError:
            logger.warning("Cannot reach clients directory")
            return

        # Initialize groups file
        # Always call load_groups to validate and handle corruption
        groups_data = self.load_groups()

        # If we got defaults back and file doesn't exist, save it
        if not self.store.exists(GROUPS):
            self.save_groups(groups_data)

    def _create_default_groups(self) -> dict[str, Any]:
        """Create default groups configuration with special groups.

        Returns:
            Dict containing default groups structure
        """
        return {
            "version": "1.0",
            "groups": [],
            "special_groups": {
                "pinned": {
                    "display_order": -1,
                    "name": "Pinned",
                    "color": "#FFC107",
                    "collapsible": False,
                },
                "all": {
                    "display_order": 999,
                    "name": "All Clients",
                    "color": "#9E9E9E",
                    "collapsible": True,
                },
            },
        }

    def load_groups(self) -> dict[str, Any]:
        """Load groups configuration with corruption recovery.

        Returns:
            Dict containing groups data with structure:
            {
                "version": "1.0",
                "groups": [...],
                "special_groups": {...}
            }

        Note:
            Returns defaults if file doesn't exist or is corrupted
        """
        if not self.store.exists(GROUPS):
            logger.info("groups.json not found, creating with defaults")
            return self._create_default_groups()

        try:
            groups_data = self.store.read_json(GROUPS)
        except StoreNotFound:
            return self._create_default_groups()
        except StoreError as e:
            if _corrupt(e):
                logger.exception("Corrupted JSON in groups.json")
                self._back_up_corrupted()
            else:
                logger.exception("Unexpected error loading groups")
            return self._create_default_groups()
        except Exception:
            logger.exception("Unexpected error loading groups")
            return self._create_default_groups()
        return self._validated(groups_data)

    def _back_up_corrupted(self) -> None:
        self.store.write_bytes(GROUPS_CORRUPTED, self.store.read_bytes(GROUPS))
        logger.info(f"Corrupted file backed up to {GROUPS_CORRUPTED}")

    def _validated(self, groups_data: Any) -> dict[str, Any]:
        """The loaded document, or defaults (backing the file up) when its structure is wrong."""
        if groups_data is None:
            return self._create_default_groups()
        try:
            if "version" not in groups_data or "groups" not in groups_data:
                logger.warning("Invalid groups.json structure, recreating")
                self._back_up_corrupted()
                return self._create_default_groups()
        except Exception:
            logger.exception("Unexpected error loading groups")
            return self._create_default_groups()
        return groups_data

    def save_groups(self, groups_data: dict[str, Any]) -> bool:
        """Save groups configuration atomically, with a backup.

        Last-writer-wins between warehouse PCs -- see ADR 0008.

        Args:
            groups_data: Groups configuration dict

        Returns:
            bool: True if saved successfully

        Raises:
            GroupsManagerError: If the write fails
        """
        self._prepare_save(groups_data)

        try:
            self.store.write_json(GROUPS, groups_data)
        # the write re-raises whatever failed, which is not always OSError.
        except Exception as e:
            error_msg = f"Failed to save groups configuration: {e}"
            logger.exception(error_msg)
            raise GroupsManagerError(error_msg) from e

        logger.info("Groups configuration saved")
        return True

    def _prepare_save(self, groups_data: dict[str, Any]) -> None:
        """Back up the current file and stamp the document that replaces it."""
        if self.store.exists(GROUPS):
            self._create_backup()
        groups_data["last_updated"] = datetime.now().astimezone().isoformat()

    def _create_backup(self) -> None:
        """Create backup of groups.json before destructive operations.

        Keeps last 10 backups. Names carry a sortable timestamp, so name order is age order.
        """
        if not self.store.exists(GROUPS):
            return

        timestamp = datetime.now().astimezone().strftime("%Y%m%d_%H%M%S")
        name = f"groups_{timestamp}.json"

        try:
            self.store.write_bytes(f"{BACKUPS}/{name}", self.store.read_bytes(GROUPS))
            logger.info(f"Backup created: {name}")

            # Keep only last 10 backups
            backups = sorted(
                (n for n in self.store.list_dir(BACKUPS) if n.startswith("groups_") and n.endswith(".json")),
                reverse=True,
            )
            for old_backup in backups[10:]:
                self.store.delete(f"{BACKUPS}/{old_backup}")
                logger.debug(f"Removed old backup: {old_backup}")

        except Exception as e:
            logger.warning(f"Failed to create backup: {e}")

    def _modify(self, mutate):
        """Load-modify-save under the store's lock: ``mutate(groups_data)`` returns the call's result.

        save_groups() alone would let two near-simultaneous create/update/delete calls each
        load the same stale snapshot and the second save silently clobber the first.
        """
        result = []

        def fn(current):
            groups_data = self._validated(current)
            result.append(mutate(groups_data))
            self._prepare_save(groups_data)
            return groups_data

        try:
            try:
                self.store.update_json(GROUPS, fn)
            except StoreError as e:
                if not _corrupt(e):
                    raise
                logger.exception("Corrupted JSON in groups.json")
                self._back_up_corrupted()
                self.store.delete(GROUPS)
                result.clear()
                self.store.update_json(GROUPS, fn)
        except StoreError as e:
            error_msg = f"Failed to save groups configuration: {e}"
            logger.exception(error_msg)
            raise GroupsManagerError(error_msg) from e
        logger.info("Groups configuration saved")
        return result[-1]

    @staticmethod
    def _name_collides_with_special_group(
        groups_data: dict[str, Any], name: str
    ) -> bool:
        special_groups = groups_data.get("special_groups", {})
        return any(
            special.get("name", "").lower() == name.lower()
            for special in special_groups.values()
        )

    def create_group(self, name: str, color: str = "#2196F3") -> str:
        """Create new group.

        Args:
            name: Group name (must be unique)
            color: Hex color code (default: Material Blue)

        Returns:
            str: Generated group UUID

        Raises:
            GroupsManagerError: If name is empty or duplicate
        """
        if not name or not name.strip():
            raise GroupsManagerError("Group name cannot be empty")

        name = name.strip()

        def mutate(groups_data):
            # Check for duplicate name (including built-in special groups)
            if self._name_collides_with_special_group(groups_data, name):
                raise GroupsManagerError(f"Group with name '{name}' already exists")
            for group in groups_data.get("groups", []):
                if group.get("name", "").lower() == name.lower():
                    raise GroupsManagerError(f"Group with name '{name}' already exists")

            # Generate UUID
            group_id = str(uuid.uuid4())

            # Calculate display_order (append to end)
            max_order = -1
            for group in groups_data.get("groups", []):
                order = group.get("display_order", 0)
                max_order = max(max_order, order)
            display_order = max_order + 1

            # Create new group
            new_group = {
                "id": group_id,
                "name": name,
                "color": color,
                "display_order": display_order,
                "created_at": datetime.now().astimezone().isoformat(),
            }

            groups_data["groups"].append(new_group)
            return group_id

        group_id = self._modify(mutate)
        logger.info(f"Group created: {name} (ID: {group_id})")
        return group_id

    def update_group(
        self, group_id: str, name: str | None = None, color: str | None = None
    ) -> bool:
        """Update existing group.

        Args:
            group_id: Group UUID
            name: New name (optional, must be unique)
            color: New color (optional, hex code)

        Returns:
            bool: True if updated successfully

        Raises:
            GroupsManagerError: If group doesn't exist or name is duplicate
        """
        def mutate(groups_data):
            nonlocal name
            # Find group
            target_group = None
            for group in groups_data.get("groups", []):
                if group.get("id") == group_id:
                    target_group = group
                    break

            if target_group is None:
                raise GroupsManagerError(f"Group with ID '{group_id}' not found")

            # Check for duplicate name if updating name
            if name is not None:
                name = name.strip()
                if not name:
                    raise GroupsManagerError("Group name cannot be empty")

                if self._name_collides_with_special_group(groups_data, name):
                    raise GroupsManagerError(f"Group with name '{name}' already exists")
                for group in groups_data.get("groups", []):
                    if (
                        group.get("id") != group_id
                        and group.get("name", "").lower() == name.lower()
                    ):
                        raise GroupsManagerError(
                            f"Group with name '{name}' already exists"
                        )

                target_group["name"] = name

            # Update color if provided
            if color is not None:
                target_group["color"] = color

        self._modify(mutate)
        logger.info(f"Group updated: {group_id}")
        return True

    def delete_group(self, group_id: str, profile_manager=None) -> bool:
        """Delete group and unassign all clients.

        Args:
            group_id: Group UUID to delete
            profile_manager: Optional ProfileManager for client updates.
                           If None, only deletes group (useful for testing).

        Returns:
            bool: True if deleted successfully

        Raises:
            GroupsManagerError: If special group or doesn't exist
        """
        # Validate not special group
        if group_id in ["pinned", "all"]:
            raise GroupsManagerError(f"Cannot delete special group: {group_id}")

        def mutate(groups_data):
            # Check if group exists
            group_exists = any(
                group.get("id") == group_id for group in groups_data.get("groups", [])
            )
            if not group_exists:
                raise GroupsManagerError(f"Group with ID '{group_id}' not found")

            # Unassign clients from this group
            if profile_manager:
                clients_in_group = self.get_clients_in_group(group_id, profile_manager)

                for client_id in clients_in_group:
                    try:
                        config = profile_manager.load_client_config(client_id)
                        if (
                            config
                            and "ui_settings" in config
                            and config["ui_settings"].get("group_id") == group_id
                        ):
                            config["ui_settings"]["group_id"] = None
                            profile_manager.save_client_config(client_id, config)
                            logger.info(
                                f"Unassigned CLIENT_{client_id} from group {group_id}"
                            )
                    except Exception:
                        logger.exception(f"Failed to unassign CLIENT_{client_id}")
                        # Continue with other clients

            # Remove group from groups.json
            groups_data["groups"] = [
                g for g in groups_data["groups"] if g.get("id") != group_id
            ]

        self._modify(mutate)
        logger.info(f"Group deleted: {group_id}")
        return True

    def get_group(self, group_id: str) -> dict[str, Any] | None:
        """Get group by ID.

        Args:
            group_id: Group UUID

        Returns:
            Dict with group data or None if not found
        """
        groups_data = self.load_groups()

        for group in groups_data.get("groups", []):
            if group.get("id") == group_id:
                return group

        return None

    def list_groups(self) -> list[dict[str, Any]]:
        """List all groups sorted by display_order.

        Returns:
            List of group dicts, sorted by display_order (ascending)
        """
        groups_data = self.load_groups()
        groups = groups_data.get("groups", [])

        # Sort by display_order
        sorted_groups = sorted(groups, key=lambda g: g.get("display_order", 0))

        return sorted_groups

    def get_clients_in_group(self, group_id: str, profile_manager) -> list[str]:
        """Get list of client IDs assigned to a group.

        Args:
            group_id: Group UUID
            profile_manager: ProfileManager instance for scanning clients

        Returns:
            List of client IDs (e.g., ["M", "ABC"])
        """
        clients_in_group = []
        all_clients = profile_manager.list_clients()

        for client_id in all_clients:
            try:
                config = profile_manager.load_client_config(client_id)
                if (
                    config
                    and "ui_settings" in config
                    and config["ui_settings"].get("group_id") == group_id
                ):
                    clients_in_group.append(client_id)
            except Exception as e:
                logger.warning(f"Failed to check group for CLIENT_{client_id}: {e}")
                continue

        return clients_in_group
