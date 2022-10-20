# Copyright 2021 Erfan Abdi
# SPDX-License-Identifier: GPL-3.0-or-later
from shutil import which
import logging
import os
import time
import glob
import signal
import sys
import uuid
import tools.config
from tools import helpers
from tools import services


def start(args):
    def set_permissions(perm_list=None, mode="777"):
        def chmod(path, mode):
            if os.path.exists(path):
                command = ["chmod", mode, "-R", path]
                tools.helpers.run.user(args, command, check=False)

        # Nodes list
        if not perm_list:
            perm_list = [
                "/dev/ashmem",

                # sw_sync for HWC
                "/dev/sw_sync",
                "/sys/kernel/debug/sync/sw_sync",

                # Media
                "/dev/Vcodec",
                "/dev/MTK_SMI",
                "/dev/mdp_sync",
                "/dev/mtk_cmdq",

                # Graphics
                "/dev/dri",
                "/dev/graphics",
                "/dev/pvr_sync",
                "/dev/ion",

                # Camera
                "/dev/media0",
                "/dev/media1",
                "/dev/media2",
                "/dev/media3",
                "/dev/media4",
                "/dev/media5",
                "/dev/media6",
                "/dev/media7",
                "/dev/media8",
                "/dev/media9",
                "/dev/v4l-subdev0",
                "/dev/v4l-subdev1",
                "/dev/v4l-subdev2",
                "/dev/v4l-subdev3",
                "/dev/v4l-subdev4",
                "/dev/v4l-subdev5",
                "/dev/v4l-subdev6",
                "/dev/v4l-subdev7",
                "/dev/v4l-subdev8",
                "/dev/v4l-subdev9",
            ]

            # Framebuffers
            perm_list.extend(glob.glob("/dev/fb*"))
            # Videos
            perm_list.extend(glob.glob("/dev/video*"))

        for path in perm_list:
            chmod(path, mode)

    def signal_handler(sig, frame):
        services.hardware_manager.stop(args)
        stop(args)
        sys.exit(0)

    status = helpers.lxc.status(args)
    if status == "STOPPED":
        # Load binder and ashmem drivers
        cfg = tools.config.load(args)
        if cfg["waydroid"]["vendor_type"] == "MAINLINE":
            if helpers.drivers.probeBinderDriver(args) != 0:
                logging.error("Failed to load Binder driver")
            helpers.drivers.probeAshmemDriver(args)
        helpers.drivers.loadBinderNodes(args)
        set_permissions([
            "/dev/" + args.BINDER_DRIVER,
            "/dev/" + args.VNDBINDER_DRIVER,
            "/dev/" + args.HWBINDER_DRIVER
        ], "666")

        if os.path.exists(tools.config.session_defaults["config_path"]):
            session_cfg = tools.config.load_session()
            if session_cfg["session"]["state"] != "STOPPED":
                logging.warning("Found session config on state: {}, restart session".format(
                    session_cfg["session"]["state"]))
                os.remove(tools.config.session_defaults["config_path"])
        logging.debug("Container manager is waiting for session to load")
        while not os.path.exists(tools.config.session_defaults["config_path"]):
            time.sleep(1)
        
        # Load session configs
        session_cfg = tools.config.load_session()

        # Networking
        command = [tools.config.tools_src +
                   "/data/scripts/waydroid-net.sh", "start"]
        tools.helpers.run.user(args, command, check=False)

        # Sensors
        if which("waydroid-sensord"):
            tools.helpers.run.user(
                args, ["waydroid-sensord", "/dev/" + args.HWBINDER_DRIVER], output="background")

        # Mount rootfs
        helpers.images.mount_rootfs(args, cfg["waydroid"]["images_path"])

        helpers.protocol.set_aidl_version(args)

        # Mount data
        helpers.mount.bind(args, session_cfg["session"]["waydroid_data"],
                           tools.config.defaults["data"])

        # Cgroup hacks
        if which("start"):
            command = ["start", "cgroup-lite"]
            tools.helpers.run.user(args, command, check=False)
        if os.path.ismount("/sys/fs/cgroup/schedtune"):
            command = ["umount", "-l", "/sys/fs/cgroup/schedtune"]
            tools.helpers.run.user(args, command, check=False)

        #TODO: remove NFC hacks
        if which("stop"):
            command = ["stop", "nfcd"]
            tools.helpers.run.user(args, command, check=False)

        # Set permissions
        set_permissions()
        
        helpers.lxc.start(args)
        session_cfg["session"]["state"] = helpers.lxc.status(args)
        timeout = 10
        while session_cfg["session"]["state"] != "RUNNING" and timeout > 0:
            session_cfg["session"]["state"] = helpers.lxc.status(args)
            logging.info(
                "waiting {} seconds for container to start...".format(timeout))
            timeout = timeout - 1
            time.sleep(1)
        if session_cfg["session"]["state"] != "RUNNING":
            raise OSError("container failed to start")
        tools.config.save_session(session_cfg)

        services.hardware_manager.start(args)

        signal.signal(signal.SIGINT, signal_handler)
        while os.path.exists(tools.config.session_defaults["config_path"]):
            session_cfg = tools.config.load_session()
            if session_cfg["session"]["state"] == "STOPPED":
                services.hardware_manager.stop(args)
                sys.exit(0)
            elif session_cfg["session"]["state"] == "UNFREEZE":
                session_cfg["session"]["state"] = helpers.lxc.status(args)
                tools.config.save_session(session_cfg)
                unfreeze(args)
            time.sleep(1)

        logging.warning("session manager stopped, stopping container and waiting...")
        stop(args)
        services.hardware_manager.stop(args)
        start(args)
    else:
        logging.error("WayDroid container is {}".format(status))

def stop(args):
    status = helpers.lxc.status(args)
    if status != "STOPPED":
        helpers.lxc.stop(args)
        if os.path.exists(tools.config.session_defaults["config_path"]):
            session_cfg = tools.config.load_session()
            session_cfg["session"]["state"] = helpers.lxc.status(args)
            tools.config.save_session(session_cfg)

        # Networking
        command = [tools.config.tools_src +
                   "/data/scripts/waydroid-net.sh", "stop"]
        tools.helpers.run.user(args, command, check=False)

        #TODO: remove NFC hacks
        if which("start"):
            command = ["start", "nfcd"]
            tools.helpers.run.user(args, command, check=False)

        # Sensors
        if which("waydroid-sensord"):
            command = ["pidof", "waydroid-sensord"]
            pid = tools.helpers.run.user(args, command, check=False, output_return=True).strip()
            if pid:
                command = ["kill", "-9", pid]
                tools.helpers.run.user(args, command, check=False)

        # Umount rootfs
        helpers.images.umount_rootfs(args)

        # Umount data
        helpers.mount.umount_all(args, tools.config.defaults["data"])

    else:
        logging.error("WayDroid container is {}".format(status))

def restart(args):
    status = helpers.lxc.status(args)
    if status == "RUNNING":
        helpers.lxc.stop(args)
        helpers.lxc.start(args)
    else:
        logging.error("WayDroid container is {}".format(status))

def freeze(args):
    status = helpers.lxc.status(args)
    if status == "RUNNING":
        helpers.lxc.freeze(args)
        if os.path.exists(tools.config.session_defaults["config_path"]):
            session_cfg = tools.config.load_session()
            session_cfg["session"]["state"] = helpers.lxc.status(args)
            tools.config.save_session(session_cfg)
    else:
        logging.error("WayDroid container is {}".format(status))

def unfreeze(args):
    status = helpers.lxc.status(args)
    if status == "FROZEN":
        helpers.lxc.unfreeze(args)
        if os.path.exists(tools.config.session_defaults["config_path"]):
            session_cfg = tools.config.load_session()
            session_cfg["session"]["state"] = helpers.lxc.status(args)
            tools.config.save_session(session_cfg)
    else:
        logging.error("WayDroid container is {}".format(status))
