"""Session Manager for Shopify Fulfillment Tool.

This module manages the lifecycle of client-specific fulfillment sessions.
It handles session creation, directory organization, and metadata management.

Key Features:
    - Create timestamped session directories ({YYYY-MM-DD_N})
    - Automatic creation of session subdirectories
    - Session metadata management via session_info.json
    - List and query existing sessions
    - Update session status and metadata

Directory Structure:
    Sessions/CLIENT_{ID}/{YYYY-MM-DD_N}/
        ├── session_info.json       # Session metadata
        ├── input/                  # Source files (orders.csv, stock.csv)
        ├── analysis/               # Analysis results and reports
        ├── packing_lists/          # Generated packing lists per courier
        └── stock_exports/          # Stock writeoff exports
"""

import contextlib
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import ClassVar

from shared.store import Store, StoreNotFound
from shopify_tool.session_state import session_rel

logger = logging.getLogger("ShopifyToolLogger")

SESSIONS = "Sessions"


class SessionManagerError(Exception):
    """Base exception for SessionManager errors."""


class _SessionMissing(Exception):
    """Raised inside an edit when the session has no session_info.json."""


class SessionManager:
    """Manages the lifecycle of client-specific fulfillment sessions.

    This class handles:
    - Creating new sessions with unique timestamped names
    - Setting up session directory structure
    - Managing session metadata (session_info.json)
    - Listing and querying sessions
    - Updating session status

    Attributes:
        profile_manager: ProfileManager instance for client checks and metadata cache
        store: Store holding the Sessions/ tree
    """

    # Session subdirectories
    SESSION_SUBDIRS: ClassVar[list[str]] = [
        "input",
        "analysis",
        "packing_lists",
        "stock_exports",
        "reference_labels",  # For PDF reference label processing
        "barcodes"  # NEW: For barcode labels (Feature #5)
    ]

    # Valid session statuses
    VALID_STATUSES: ClassVar[list[str]] = ["active", "completed", "abandoned", "archived"]

    def __init__(self, profile_manager, store: Store):
        """Initialize SessionManager.

        Args:
            profile_manager: ProfileManager, for client checks and the metadata cache
            store: Store holding the Sessions/ tree
        """
        self.profile_manager = profile_manager
        self.store = store

        logger.info("SessionManager initialized")

    @staticmethod
    def _client_rel(client_id: str) -> str:
        return f"{SESSIONS}/CLIENT_{client_id.upper()}"

    def create_session(self, client_id: str) -> str:
        """Create a new session for a client.

        Creates a timestamped directory with format {YYYY-MM-DD_N} where N is
        an incrementing number for multiple sessions on the same day.

        Also creates:
        - Session subdirectories (input/, analysis/, etc.)
        - session_info.json with initial metadata

        Args:
            client_id (str): Client ID (e.g., "M")

        Returns:
            str: Full path to created session directory

        Raises:
            SessionManagerError: If session creation fails
        """
        client_id = client_id.upper()

        # Verify client exists
        if not self.profile_manager.client_exists(client_id):
            raise SessionManagerError(f"Client does not exist: CLIENT_{client_id}")

        client_rel = self._client_rel(client_id)

        # claim_dir is the claim. Two PCs can derive one name
        # from a stale listing (SMB caches it ~10 s) and only one claim wins;
        # the loser takes the next number rather than touching that folder
        # (AUDIT-05-1). Counting on from the name, not re-listing, because
        # the listing is what was stale.
        date, _, number = self._generate_unique_session_name(
            client_rel
        ).rpartition("_")
        for n in range(int(number), int(number) + 10):
            rel = f"{client_rel}/{date}_{n}"
            if self.store.claim_dir(rel):
                break
        else:
            raise SessionManagerError(
                f"No free session name for CLIENT_{client_id} on {date}"
            )
        session_path = self.store.local_path(rel)
        session_name = session_path.name

        # Past this point the folder is ours, so the cleanup below can only
        # ever remove what this call created.
        try:
            # Create subdirectories
            for subdir in self.SESSION_SUBDIRS:
                if not self.store.claim_dir(f"{rel}/{subdir}"):
                    raise FileExistsError(f"{rel}/{subdir}")

            # Create session_info.json
            session_info = {
                "created_by_tool": "shopify",
                "created_at": datetime.now().astimezone().isoformat(),
                "client_id": client_id,
                "session_name": session_name,
                "status": "active",
                "pc_name": os.environ.get('COMPUTERNAME', 'Unknown'),
                "orders_file": None,
                "stock_file": None,
                "analysis_completed": False,
                "packing_lists_generated": [],
                "stock_exports_generated": [],
                "statistics": {
                    "total_orders": 0,
                    "total_items": 0,
                    "packing_lists_count": 0,
                    "packing_lists": []
                },
                "comments": "",
                "last_modified": datetime.now().astimezone().isoformat()
            }

            self.store.write_json(f"{rel}/session_info.json", session_info)

            self.profile_manager.invalidate_metadata_cache(client_id)

            logger.info(f"Session created: CLIENT_{client_id}/{session_name}")
            return str(session_path)

        except Exception as e:
            logger.exception("Failed to create session")
            # Cleanup on failure
            with contextlib.suppress(Exception):
                self.store.delete(rel)
            raise SessionManagerError(f"Failed to create session: {e}")

    def _generate_unique_session_name(self, client_rel: str) -> str:
        """Generate unique session name with format {YYYY-MM-DD_N}.

        Finds the next available number for today's date.

        Args:
            client_rel (str): Store path of the client's sessions folder

        Returns:
            str: Unique session name (e.g., "2025-11-05_1")
        """
        today = datetime.now().astimezone().strftime("%Y-%m-%d")

        # Find existing sessions for today; session folders have no dot, files do
        existing_sessions = [
            name
            for name in self.store.list_dir(client_rel)
            if name.startswith(today) and "." not in name
        ]

        # Find next available number
        if not existing_sessions:
            return f"{today}_1"

        # Extract numbers from existing sessions
        numbers = []
        for session_name in existing_sessions:
            try:
                # Format: YYYY-MM-DD_N
                parts = session_name.split('_')
                if len(parts) >= 2:  # Should have at least date_number
                    # Last part should be the number
                    number = int(parts[-1])
                    numbers.append(number)
            except (ValueError, IndexError):
                continue

        # Get next number
        next_number = max(numbers) + 1 if numbers else 1
        return f"{today}_{next_number}"

    def get_session_path(self, client_id: str, session_name: str) -> Path:
        """Get full path to a session directory.

        Args:
            client_id (str): Client ID
            session_name (str): Session name (e.g., "2025-11-05_1")

        Returns:
            Path: Full path to session directory
        """
        return self.store.local_path(f"{self._client_rel(client_id)}/{session_name}")

    def list_client_sessions(
        self,
        client_id: str,
        status_filter: str | None = None
    ) -> list[dict]:
        """List all sessions for a client, newest first.

        The Store answers with each session's session_info (query_sessions).
        This adds the session path, and the fields older sessions lack.

        Args:
            client_id (str): Client ID
            status_filter (str, optional): Filter by status ("active", "completed", etc.)

        Returns:
            List[Dict]: session_info dicts sorted by created_at (newest first), each
                with session_name and session_path
        """
        client_id = client_id.upper()
        client_rel = self._client_rel(client_id)

        sessions = []
        for session_info in self.store.query_sessions(client_id):
            if status_filter and session_info.get("status") != status_filter:
                continue
            session_path = str(self.store.local_path(f"{client_rel}/{session_info['session_name']}"))
            session_info["session_path"] = session_path
            session_info.setdefault("comments", "")
            if "statistics" not in session_info:  # legacy session, before statistics were stored
                session_info["statistics"] = self.calculate_session_statistics(session_path)
            sessions.append(session_info)

        sessions.sort(key=lambda x: x.get("created_at", ""), reverse=True)
        return sessions

    def _info_rel(self, session_path) -> str:
        return f"{session_rel(self.store, session_path)}/session_info.json"

    def _normalised(self, session_info: dict, session_path) -> dict:
        """Add the fields get_session_info gives: session_path, statistics, comments."""
        # Add full path to info
        session_info["session_path"] = str(Path(session_path))

        # Calculate statistics if missing (backwards compatibility)
        if "statistics" not in session_info:
            session_info["statistics"] = self.calculate_session_statistics(session_path)

        # Ensure comments field exists
        if "comments" not in session_info:
            session_info["comments"] = ""

        return session_info

    def _edit_info(self, session_path, edit, failed: str) -> dict | None:
        """Read-modify-write session_info.json under its lock.

        `edit` gets the session's info as get_session_info sees it and returns
        the dict to write. Returns the dict written, or None when the session
        has no session_info.json. Any other failure is logged under `failed`
        and raised as SessionManagerError.
        """

        def change(raw):
            if raw is None:
                raise _SessionMissing()
            info = edit(self._normalised(raw, session_path))
            info.pop("session_path", None)  # computed, never stored
            return info

        folder = session_rel(self.store, session_path)
        if not self.store.exists(folder):
            return None  # update_json would re-create a deleted folder
        try:
            return self.store.update_json(f"{folder}/session_info.json", change)
        except _SessionMissing:
            return None
        except Exception as e:
            logger.exception(failed)
            raise SessionManagerError(f"{failed}: {e}") from e

    def get_session_info(self, session_path: str) -> dict | None:
        """Load session metadata from session_info.json.

        Args:
            session_path (str): Full path to session directory

        Returns:
            Optional[Dict]: Session info dictionary or None if not found/invalid
        """
        try:
            session_info = self.store.read_json(self._info_rel(session_path))
            return self._normalised(session_info, session_path)
        except StoreNotFound:
            logger.warning(f"Session info not found: {session_path}")
            return None
        except Exception:
            logger.exception("Failed to load session info")
            return None

    def update_session_status(self, session_path: str, status: str, manual: bool = False) -> bool:
        """Update session status in session_info.json.

        Args:
            session_path (str): Full path to session directory
            status (str): New status ("active", "completed", "abandoned")
            manual (bool): True when a human set this status. Records
                `status_manually_set`, which stops session_lifecycle from
                ever managing this session's status again.

        Returns:
            bool: True if updated successfully

        Raises:
            SessionManagerError: If status is invalid or update fails
        """
        if status not in self.VALID_STATUSES:
            raise SessionManagerError(
                f"Invalid status: {status}. Must be one of {self.VALID_STATUSES}"
            )

        def edit(info):
            info["status"] = status
            info["status_updated_at"] = datetime.now().astimezone().isoformat()
            if manual:
                info["status_manually_set"] = True
            return info

        session_info = self._edit_info(session_path, edit, "Failed to update session status")
        if session_info is None:
            raise SessionManagerError(f"Session not found: {session_path}")

        logger.info(f"Session status updated to '{status}': {session_path}")
        return True

    def apply_status_updates(self, client_id: str, updates: dict) -> int:
        """Set many sessions' statuses, one session_info.json write each.

        Each write takes its own lock. That lock serializes this class against
        itself only: Packing Tool's update_session_metadata() read-modify-writes
        the same file without taking the sidecar lock, so its packing_progress
        write and this status write can still lose each other in the few ms
        between read and write. Closing it properly means teaching Packing Tool
        to take the same lock.

        Best-effort per session: one unwritable session is logged and skipped
        and the rest still apply. Never raises; a session list that will not
        load is worse than one carrying a stale status.

        Returns the number of sessions actually updated.
        """
        if not updates:
            return 0

        client_sessions_dir = self.store.local_path(self._client_rel(client_id))
        applied = 0

        for session_name, status in updates.items():
            if status not in self.VALID_STATUSES:
                logger.warning(f"Skipping invalid status '{status}' for {session_name}")
                continue
            session_path_obj = client_sessions_dir / session_name

            def edit(info, status=status):
                info["status"] = status
                info["status_updated_at"] = datetime.now().astimezone().isoformat()
                return info

            # Atomic: this path writes ~N files unattended (41 on the
            # first archive pass). A torn session_info.json makes
            # get_session_info return None, which drops the session
            # out of the browser entirely. The Store writes it atomically under its lock.
            try:
                session_info = self._edit_info(
                    session_path_obj, edit, f"Failed to apply status to {session_path_obj}"
                )
            except SessionManagerError:
                continue  # already logged
            if session_info is None:
                logger.warning(f"Skipping missing session: {session_path_obj}")
                continue
            applied += 1

        logger.info(f"Applied {applied} automatic status updates for CLIENT_{client_id}")
        return applied

    def restore_session_fields(self, session_path: str, fields: dict) -> bool:
        """Put stored fields back exactly: the Browse page's Undo.

        Each key in `fields` is set to its value; a value of None removes the
        key. Nothing is stamped, so `last_updated` and `status_updated_at` end
        up as the caller hands them in -- a session that was Stale before the
        change is Stale again after the Undo.

        Args:
            session_path (str): Full path to session directory
            fields (Dict): The fields to restore; None removes a key

        Returns:
            bool: True if restored successfully

        Raises:
            SessionManagerError: If a status is invalid, the session is not
                found, or the write fails
        """
        status = fields.get("status")
        if status is not None and status not in self.VALID_STATUSES:
            raise SessionManagerError(
                f"Invalid status: {status}. Must be one of {self.VALID_STATUSES}"
            )

        def edit(info):
            for key, value in fields.items():
                if value is None:
                    info.pop(key, None)
                else:
                    info[key] = value
            return info

        session_info = self._edit_info(session_path, edit, "Failed to restore session fields")
        if session_info is None:
            raise SessionManagerError(f"Session not found: {session_path}")

        logger.info(f"Session fields restored: {session_path}")
        return True

    def update_session_info(self, session_path: str, updates: dict) -> bool:
        """Update session metadata with arbitrary fields.

        Args:
            session_path (str): Full path to session directory
            updates (Dict): Dictionary of fields to update

        Returns:
            bool: True if updated successfully

        Raises:
            SessionManagerError: If update fails
        """
        def edit(info):
            # Apply updates
            info.update(updates)
            info["last_updated"] = datetime.now().astimezone().isoformat()
            return info

        session_info = self._edit_info(session_path, edit, "Failed to update session info")
        if session_info is None:
            raise SessionManagerError(f"Session not found: {session_path}")

        logger.info(f"Session info updated: {session_path}")
        return True

    def append_to_session_list(self, session_path: str, field: str, value) -> bool:
        """Atomically append value to a list field in session_info.json.

        update_session_info()'s lock only protects its own read-modify-write;
        a caller that reads the list itself (e.g. via get_session_info),
        appends locally, and then calls update_session_info() with the whole
        new list is still racing every other caller doing the same for a
        DIFFERENT value on the same field (e.g. two packing lists generated
        back to back) -- each reads a list that doesn't yet contain the
        other's addition, and whichever writes last wins. Reading the list
        fresh inside this method's own lock closes that gap.

        Returns:
            bool: True if value was appended, False if it was already present
        """
        appended = False

        def edit(info):
            nonlocal appended
            current_list = info.get(field, [])
            if value in current_list:
                return info  # written back unchanged; see the caller below
            appended = True
            info[field] = current_list + [value]
            info["last_updated"] = datetime.now().astimezone().isoformat()
            return info

        session_info = self._edit_info(session_path, edit, "Failed to update session info")
        if session_info is None:
            raise SessionManagerError(f"Session not found: {session_path}")
        if not appended:
            return False

        logger.info(f"Session info updated: appended '{value}' to '{field}'")
        return True

    def get_session_subdirectory(self, session_path: str, subdir_name: str) -> Path:
        """Get path to a session subdirectory.

        Args:
            session_path (str): Full path to session directory
            subdir_name (str): Subdirectory name ("input", "analysis", etc.)

        Returns:
            Path: Full path to subdirectory

        Raises:
            SessionManagerError: If subdirectory doesn't exist
        """
        if subdir_name not in self.SESSION_SUBDIRS:
            raise SessionManagerError(
                f"Invalid subdirectory: {subdir_name}. "
                f"Must be one of {self.SESSION_SUBDIRS}"
            )

        rel = f"{session_rel(self.store, session_path)}/{subdir_name}"

        if not self.store.exists(rel):
            raise SessionManagerError(f"Subdirectory not found: {self.store.local_path(rel)}")

        return self.store.local_path(rel)

    def get_input_dir(self, session_path: str) -> Path:
        """Get path to session input directory."""
        return self.get_session_subdirectory(session_path, "input")

    def get_analysis_dir(self, session_path: str) -> Path:
        """Get path to session analysis directory."""
        return self.get_session_subdirectory(session_path, "analysis")

    def get_packing_lists_dir(self, session_path: str) -> Path:
        """Get path to session packing_lists directory."""
        return self.get_session_subdirectory(session_path, "packing_lists")

    def get_stock_exports_dir(self, session_path: str) -> Path:
        """Get path to session stock_exports directory."""
        return self.get_session_subdirectory(session_path, "stock_exports")

    def get_reference_labels_dir(self, session_path: str) -> Path:
        """
        Get path to session reference_labels directory.

        Args:
            session_path: Session path

        Returns:
            Path: Path to reference_labels subdirectory

        Example:
            >>> manager.get_reference_labels_dir("Sessions/CLIENT_M/2025-01-15_1")
            Path("Sessions/CLIENT_M/2025-01-15_1/reference_labels")
        """
        return self.get_session_subdirectory(session_path, "reference_labels")

    def get_barcodes_dir(self, session_path: str) -> Path:
        """
        Get path to session barcodes directory.

        Args:
            session_path: Session path

        Returns:
            Path: Path to barcodes subdirectory

        Example:
            >>> sm.get_barcodes_dir("Sessions/CLIENT_M/2026-01-16_1")
            Path("Sessions/CLIENT_M/2026-01-16_1/barcodes")
        """
        return self.store.local_path(f"{session_rel(self.store, session_path)}/barcodes")

    def get_packing_list_barcode_dir(self, session_path: str, packing_list_name: str) -> Path:
        """
        Get path to barcode directory for specific packing list.

        Each packing list has its own barcode subdirectory to organize labels.

        Args:
            session_path: Session path
            packing_list_name: Name of packing list (e.g., "DHL_Orders")

        Returns:
            Path: Path to packing list's barcode subdirectory

        Example:
            >>> sm.get_packing_list_barcode_dir("Sessions/CLIENT_M/2026-01-16_1", "DHL_Orders")
            Path("Sessions/CLIENT_M/2026-01-16_1/barcodes/DHL_Orders")
        """
        return self.get_barcodes_dir(session_path) / packing_list_name

    def get_barcode_history_file(self, session_path: str, packing_list_name: str) -> Path:
        """
        Get path to barcode history JSON file for specific packing list.

        Args:
            session_path: Session path
            packing_list_name: Name of packing list

        Returns:
            Path: Path to barcode_history.json
        """
        return self.get_packing_list_barcode_dir(session_path, packing_list_name) / "barcode_history.json"

    def session_exists(self, client_id: str, session_name: str) -> bool:
        """Check if a session exists.

        Args:
            client_id (str): Client ID
            session_name (str): Session name

        Returns:
            bool: True if session exists
        """
        return self.store.exists(f"{self._client_rel(client_id)}/{session_name}")

    def delete_session(self, session_path: str) -> bool:
        """Delete a session directory.

        WARNING: This permanently deletes all session data.

        Args:
            session_path (str): Full path to session directory

        Returns:
            bool: True if deleted successfully

        Raises:
            SessionManagerError: If deletion fails
        """
        session_path_obj = Path(session_path).resolve()

        try:
            inside = session_path_obj.relative_to(self.store.local_path(SESSIONS).resolve())
        except ValueError:
            raise SessionManagerError(
                f"Refusing to delete path outside sessions root: {session_path}"
            )
        rel = f"{SESSIONS}/{inside.as_posix()}"

        if not self.store.exists(rel):
            logger.warning(f"Session not found for deletion: {session_path}")
            return False

        try:
            self.store.delete(rel)
            logger.info(f"Session deleted: {session_path}")
            return True

        except Exception as e:
            logger.exception("Failed to delete session")
            raise SessionManagerError(f"Failed to delete session: {e}")

    def calculate_session_statistics(self, session_path: str) -> dict:
        """Calculate session statistics by scanning session directory.

        Reads analysis_data.json for orders/items count and scans packing_lists
        directory for generated packing lists.

        Args:
            session_path (str): Full path to session directory

        Returns:
            dict: {
                "total_orders": int,
                "total_items": int,
                "packing_lists_count": int,
                "packing_lists": list[str]
            }
        """
        statistics = {
            "total_orders": 0,
            "total_items": 0,
            "packing_lists_count": 0,
            "packing_lists": []
        }

        try:
            rel = session_rel(self.store, session_path)
            # Try to read analysis_data.json for orders/items count
            analysis_data_rel = f"{rel}/analysis/analysis_data.json"

            if self.store.exists(analysis_data_rel):
                analysis_data = self.store.read_json(analysis_data_rel)

                # Count unique orders and total items
                if isinstance(analysis_data, list):
                    statistics["total_items"] = len(analysis_data)
                    # Count unique Order_Number values
                    order_numbers = set()
                    for item in analysis_data:
                        if "Order_Number" in item:
                            order_numbers.add(item["Order_Number"])
                    statistics["total_orders"] = len(order_numbers)

            # Count packing lists (an absent folder lists as empty)
            packing_lists = [
                name[: -len(".json")]
                for name in self.store.list_dir(f"{rel}/packing_lists")
                if name.endswith(".json")
            ]
            statistics["packing_lists"] = sorted(packing_lists)
            statistics["packing_lists_count"] = len(packing_lists)

        except Exception as e:
            logger.warning(f"Failed to calculate statistics for {session_path}: {e}")

        return statistics
