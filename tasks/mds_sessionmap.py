import contextlib
import json
import logging
from tasks.cephfs.cephfs_test_case import CephFSTestCase, run_tests
from tasks.cephfs.filesystem import Filesystem

log = logging.getLogger(__name__)


class TestSessionMap(CephFSTestCase):
    def test_version_splitting(self):
        """
        That when many sessions are updated, they are correctly
        split into multiple versions to obey mds_sessionmap_keys_per_op
        """

        # Start umounted
        self.mount_a.umount_wait()
        self.mount_b.umount_wait()

        # Configure MDS to write one OMAP key at once
        self.set_conf('mds', 'mds_sessionmap_keys_per_op', 2)
        self.fs.mds_fail_restart()
        self.fs.wait_for_daemons()

        # I would like two MDSs, so that I can do an export dir later
        self.fs.mon_manager.raw_cluster_cmd_result('mds', 'set', "max_mds", "2")
        self.fs.wait_for_daemons()

        active_mds_names = self.fs.get_active_names()
        rank_0_id = active_mds_names[0]
        rank_1_id = active_mds_names[1]
        log.info("Ranks 0 and 1 are {0} and {1}".format(
            rank_0_id, rank_1_id))

        # Bring the clients back
        self.mount_a.mount()
        self.mount_b.mount()
        self.mount_a.create_files()  # Kick the client into opening sessions
        self.mount_b.create_files()

        # See that they've got sessions
        self.assert_session_count(2, mds_id=rank_0_id)

        # See that we persist their sessions
        self.fs.mds_asok(["flush", "journal"], rank_0_id)
        table_json = json.loads(self.fs.table_tool(["0", "show", "session"]))
        self.assertEqual(table_json['0']['result'], 0)
        self.assertEqual(len(table_json['0']['data']['Sessions']), 2)

        # Now, induce a "force_open_sessions" event by exporting a dir
        self.mount_a.run_shell(["mkdir", "bravo"])
        self.mount_a.run_shell(["touch", "bravo/file"])
        self.mount_b.run_shell(["ls", "-l", "bravo/file"])

        def get_omap_wrs():
            return self.fs.mds_asok(['perf', 'dump', 'objecter'], rank_1_id)['objecter']['omap_wr']

        initial_omap_wrs = get_omap_wrs()
        self.fs.mds_asok(['export', 'dir', '/bravo', '1'], rank_0_id)

        # This is the critical (if rather subtle) check: that in the process of doing an export dir,
        # we hit force_open_sessions, and as a result we end up writing out the sessionmap
        # OMAP inline rather than simply saving up the modifications.
        # The number of writes is two, because the header (sessionmap version) update and KV write both count.
        self.assertEqual(get_omap_wrs() - initial_omap_wrs, 2)


@contextlib.contextmanager
def task(ctx, config):
    fs = Filesystem(ctx)

    # Pick out the clients we will use from the configuration
    # =======================================================
    if len(ctx.mounts) < 2:
        raise RuntimeError("Need at least two clients")
    mount_a = ctx.mounts.values()[0]
    mount_b = ctx.mounts.values()[1]

    # Stash references on ctx so that we can easily debug in interactive mode
    # =======================================================================
    ctx.filesystem = fs
    ctx.mount_a = mount_a
    ctx.mount_b = mount_b

    run_tests(ctx, config, TestSessionMap, {
        'fs': fs,
        'mount_a': mount_a,
        'mount_b': mount_b
    })

    # Continue to any downstream tasks
    # ================================
    yield
