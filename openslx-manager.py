#!/usr/bin/env python2
# -*- coding: utf-8 -*-
# 2016 sschmelzer


import os
import sys
import click
import logging
import subprocess
import yaml
import pprint
import traceback

from glob import glob
from shutil import copyfile, rmtree

logging.getLogger().setLevel(logging.INFO)


def copylink(src, dst, force=False):
    if not os.path.islink(src):
        raise IOError("Source is not a link")
    if os.path.exists(dst) and not force:
        raise IOError("Destination is existing")
    if os.path.exists(dst) and force:
        os.unlink(dst)
    os.symlink(os.readlink(src),dst)


class OpenslxManager(object):
    def __init__(self):
        try:
            with open(os.path.join(os.path.dirname(os.path.realpath(__file__)), 'etc', 'config.yml')) as fh:
                self._config = yaml.load(fh)
        except IOError:
            logging.error("Didn't found config - make sure you copied config.dist.yml to config.yml.")
            sys.exit(-1)


    def cfg(self, key):
        try:
            return self._config['general'][key]
        except:
            return None


    def image_cfg(self, key, image=None):
        try:
            if not image:
                image = self.cfg('default-image')

            return self._config['images'][image][key]
        except:
            return None


    def dump_config(self):
        pprint.pprint(self._config)


    def show_default(self):
        print self.cfg('default-image')


    def run_cmd(self, cmd_args, cwd='/', shell=True):
        try:
            if shell:
                cmd_args = ' '.join(cmd_args)
            logging.info(' -- start command: %s' % cmd_args)
            proc = subprocess.Popen(
                        cmd_args,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        shell=True,
                        cwd=cwd
                    )

            proc_stdout, proc_stderr =  proc.communicate()

            if proc_stdout:
                logging.debug(proc_stdout)
            if proc_stderr:
                logging.error(proc_stderr)

            exit_code = proc.wait()
            logging.info(" -- command finished with exit-code: %s" % exit_code)

        except Exception as e:
            logging.error(" -- caught exception while executing command - msg: %s" % e)


    def get_latest_revision(self, base_name, pprint=False):
        rnum = 0
        try:
            for p in glob("%s.r*" % base_name):
                _r = int(p.split('.')[-1][1:])
                if _r > rnum:
                    rnum = _r
        except:
            pass
        if not pprint:
            return rnum
        else:
            return "r%02d" % rnum


    def calculate_new_revision(self, base_name):
        rnum = self.get_latest_revision(base_name)
        rnum = rnum + 1
        logging.info(" -- calculated new revision number for '%s': '%s'" % (base_name, rnum))
        return "%s.r%02d" % (base_name, rnum)


    def reload_dnbd3(self):
        logging.info("Reload dnbd3-server on all configured machines")
        self.run_cmd(["killall", "-USR1", "dnbd3-server"])
        for dnbdhost in self.cfg('dnbd3-servers'):
            self.run_cmd(['ssh', 'root@%s' % dnbdhost, "killall", "-USR1", "dnbd3-server"])


    def update_filesystem(self, image=None):
        logging.info("Update DNBD3 Image for System: %s" % self.image_cfg('name', image=image))
        clone_target_rsync = os.path.join(self.cfg('image-path'),'raw', self.image_cfg('name', image=image))
        clone_target_sqfs = os.path.join(self.cfg('image-path'),'sqfs', "%s.%s" % (self.image_cfg('name', image=image), 'sqfs'))
        clone_cmd = [
            os.path.join(self.cfg('openslx-base'), 'core', 'clone_stage4'),
            self.image_cfg('remote', image=image),
            clone_target_rsync
        ]

        logging.info("Start Clone (rsync) of reference machine: %s" % clone_cmd)
        self.run_cmd(clone_cmd)

        clone_target_sqfs = self.calculate_new_revision(clone_target_sqfs)
        sqfs_cmd = ['mksquashfs','*', clone_target_sqfs, '-comp', 'xz']
        logging.info("Create SQFS image: %s (this will definitely take some time..)" % sqfs_cmd)
        self.run_cmd(sqfs_cmd,cwd=clone_target_rsync)

        self.reload_dnbd3()


    def rebuild_remote(self, image=None):
        logging.info("Build stage31(initrd) and stage32 on reference machine")
        rebuild_stage31_cmd = ['ssh', 'root@%s' % self.image_cfg('remote', image=image),
                               '%s stage31 -b' % self.cfg('mltk-bin')]
        rebuild_stage32_cmd = ['ssh', 'root@%s'% self.image_cfg('remote', image=image),
                               '%s %s -b' % (self.cfg('mltk-bin'), self.image_cfg('stage32-name', image=image))]
        self.run_cmd(rebuild_stage31_cmd)
        self.run_cmd(rebuild_stage32_cmd)


    def sync_remote(self, image=None):
        logging.info("Sync data (kernel, stage31, stage32) from reference machine")
        sync_cmd = [
            '/usr/local/sbin/openslx',
            self.image_cfg('remote', image=image),
            '-s'
        ]
        self.run_cmd(sync_cmd)


    def update_runtime_config(self, image=None):
        logging.info("Build runtime config (config.tgz).")
        runtime_config_cmd = [
            '/usr/local/sbin/openslx',
            self.image_cfg('remote', image=image),
            '-k',
            self.image_cfg('config', image=image)
        ]
        self.run_cmd(runtime_config_cmd)


    def deploy_testing(self, image=None):
        logging.info("Deploy testing: copy latest build on www, tftpd shares and update testing links")
        tftpboot_files = ['kernel/kernel', 'initramfs-stage31']
        tftpboot_source = os.path.join(self.cfg('openslx-base'), 'var', 'boot', self.image_cfg('remote', image=image))
        tftpboot_target = os.path.join(self.cfg('tftpd-path'), self.image_cfg('name', image=image))
        tftpboot_target_testing = "%s.testing" % tftpboot_target
        tftpboot_target = self.calculate_new_revision(tftpboot_target)
        os.mkdir(tftpboot_target)

        for f in tftpboot_files:
            logging.info(" -- copy %s to %s" % (f, tftpboot_target))
            copyfile(
                os.path.join(tftpboot_source, f),
                os.path.join(tftpboot_target, f.split('/')[-1])
            )

        if os.path.exists(tftpboot_target_testing):
            os.unlink(tftpboot_target_testing)

        logging.info(" -- link %s to %s" % (tftpboot_target_testing, tftpboot_target))
        os.symlink(
            os.path.basename(tftpboot_target),
            tftpboot_target_testing
        )


        www_files = [
            '%s.sqfs' % self.image_cfg('stage32-name', image=image),
            '/'.join(['configs', self.image_cfg('config', image=image) ,'config.tgz'])
        ]
        www_source = tftpboot_source
        www_target = os.path.join(self.cfg('www-path'), self.image_cfg('name', image=image))
        www_target_testing = "%s.testing" % www_target
        www_target = self.calculate_new_revision(www_target)
        os.mkdir(www_target)

        for f in www_files:
            logging.info(" -- copy %s to %s" % (f, www_target))
            copyfile(
                os.path.join(www_source, f),
                os.path.join(www_target, f.split('/')[-1])
            )

        os.symlink(
            '%s.sqfs' % self.image_cfg('stage32-name', image=image),
            os.path.join(www_target, 'stage32.sqfs')
        )

        if os.path.exists(www_target_testing):
            os.unlink(www_target_testing)

        logging.info(" -- link %s to %s" % (www_target_testing, www_target))
        os.symlink(
            os.path.basename(www_target),
            www_target_testing
        )

        logging.info(" -- copy %s to %s" % ('%s.config' % self.image_cfg('config', image=image), www_target))
        copyfile(
            os.path.join(os.path.dirname(os.path.realpath(__file__)), 'etc', '%s.config' % self.image_cfg('config', image=image)),
            os.path.join(www_target, 'config')
        )


    def update_testing(self, image=None):
        logging.info("Sync and repack stage31(initrd), stage32 from reference machine; build runtime config (config.tgz).")
        self.sync_remote(image=image)

        logging.info("continue with update_testing..")

        create_initrd_cmd = [
            self.cfg('openslx-bin'),
            self.image_cfg('remote', image=image),
            'stage31',
            '-e',
            'cpio'
        ]
        self.run_cmd(create_initrd_cmd)

        create_stage32_cmd = [
            self.cfg('openslx-bin'),
            self.image_cfg('remote', image=image),
            self.image_cfg('stage32-name', image=image),
            '-e',
            'sqfs'
        ]
        self.run_cmd(create_stage32_cmd)
        self.update_runtime_config(image=image)


    def replace_in_config(self, cfg, from_string, to_string):
        tmp_cfg = '%s.tmp'% cfg
        logging.info(' -- create tmp config: %s' % tmp_cfg)
        with open(tmp_cfg, 'wt') as out_fh:
            with open(cfg, 'rt') as in_fh:
                for line in in_fh:
                    out_fh.write(line.replace(from_string, to_string))
        logging.info(' -- replace config with tmp config: %s' % cfg)
        os.unlink(cfg)
        os.rename(tmp_cfg, cfg)


    def promote_testing(self, image=None):
        logging.info("Promote testing version to stable")
        tftpboot_target = os.path.join(self.cfg('tftpd-path'), self.image_cfg('name', image=image))
        tftpboot_target_testing = "%s.testing" % tftpboot_target
        tftpboot_target_stable = "%s.stable" % tftpboot_target
        tftpboot_target_oldstable = "%s.oldstable" % tftpboot_target
        tftpboot_target_old_oldstable = self.calculate_new_revision(tftpboot_target_oldstable)

        if not os.path.exists(tftpboot_target_testing):
            return

        if os.path.exists(tftpboot_target_stable):
            if os.path.exists(tftpboot_target_oldstable):
                logging.info(" -- move %s to %s" % (tftpboot_target_oldstable, tftpboot_target_old_oldstable))
                os.rename(tftpboot_target_oldstable, tftpboot_target_old_oldstable)
            logging.info(" -- move %s to %s" % (tftpboot_target_stable, tftpboot_target_oldstable))
            os.rename(tftpboot_target_stable, tftpboot_target_oldstable)

        logging.info(" -- copy %s to %s" % (tftpboot_target_testing, tftpboot_target_stable))
        copylink(tftpboot_target_testing, tftpboot_target_stable)


        www_target = os.path.join(self.cfg('www-path'), self.image_cfg('name', image=image))
        www_target_testing = "%s.testing" % www_target
        www_target_stable = "%s.stable" % www_target
        www_target_oldstable = "%s.oldstable" % www_target
        www_target_old_oldstable = self.calculate_new_revision(www_target_oldstable)

        if not os.path.exists(www_target_testing):
            return

        if os.path.exists(www_target_stable):
            if os.path.exists(www_target_oldstable):
                logging.info(" -- move %s to %s" % (www_target_oldstable, www_target_old_oldstable))
                os.rename(www_target_oldstable, www_target_old_oldstable)
            logging.info(" -- move %s to %s" % (www_target_stable, www_target_oldstable))
            os.rename(www_target_stable, www_target_oldstable)

        logging.info(" -- copy %s to %s" % (www_target_testing, www_target_stable))
        copylink(www_target_testing, www_target_stable)


        dnbd_name = '%s.sqfs' % self.image_cfg('name', image=image)
        dnbd_name_stable = '%s-stable.sqfs' % self.image_cfg('name', image=image)
        dnbd_name_oldstable = '%s-oldstable.sqfs' % self.image_cfg('name', image=image)

        logging.info(" -- set corresponding DNBD3 image in config (for stable)")
        self.replace_in_config(os.path.join(www_target_stable,'config'), dnbd_name, dnbd_name_stable)

        logging.info(" -- set corresponding DNBD3 image in config (for oldstable)")
        if os.readlink(www_target_oldstable) == os.readlink(www_target_stable):
            logging.info(" -- skipping (oldstable points to the same config as stable)")
        else:
            self.replace_in_config(os.path.join(www_target_oldstable,'config'), dnbd_name_stable, dnbd_name_oldstable)


        dnbd_target = "%s/sqfs" % self.cfg('image-path')
        dnbd_target_testing = os.path.join(dnbd_target, dnbd_name)
        dnbd_target_testing += ".%s" % self.get_latest_revision(dnbd_target_testing,pprint=True)

        dnbd_target_stable = os.path.join(dnbd_target, dnbd_name_stable)

        dnbd_target_oldstable = os.path.join(dnbd_target, dnbd_name_oldstable)
        dnbd_target_oldstable = self.calculate_new_revision(dnbd_target_oldstable)

        if os.path.exists(dnbd_target_stable):
            logging.info(" -- copy link from %s to %s" % (dnbd_target_stable, dnbd_target_oldstable))
            copylink(dnbd_target_stable, dnbd_target_oldstable)

        dnbd_name_testing = dnbd_target_testing.split('/')[-1]
        dnbd_target_stable = self.calculate_new_revision(dnbd_target_stable)
        logging.info(" -- set stable to %s" % dnbd_name_testing)
        os.symlink(dnbd_name_testing, dnbd_target_stable)


    def cleanup_revdirs(self, base, image, tryonly):
        revdir_target = os.path.join(base, image)
        revdir_target_testing = "%s.testing" % revdir_target
        revdir_target_stable = "%s.stable" % revdir_target
        revdir_target_oldstable = "%s.oldstable" % revdir_target

        # one offset for .oldstable without revision (acvitve one)
        keep_oldstable = self.image_cfg('keep-oldstable', image=image) - 1
        keep_testing = self.image_cfg('keep-testing', image=image)

        # remove obsolete links
        revdir_delete_links = sorted(glob("%s.r*" % revdir_target_oldstable),reverse=True)[keep_oldstable:]
        for l in revdir_delete_links:
            logging.info(" -- remove link %s" % l)
            if not tryonly:
                os.unlink(l)

        revdir_link_targets = [os.readlink(x) for x in glob("%s.r*" % revdir_target_oldstable)]
        revdir_link_targets.append(os.readlink(revdir_target_stable))
        revdir_link_targets.append(os.readlink(revdir_target_oldstable))
        revdir_link_targets.append(os.readlink(revdir_target_testing))

        revdir_delete_folders = sorted(glob("%s.r*" % revdir_target),reverse=True)[keep_testing:]
        revdir_delete_folders = [ x for x in revdir_delete_folders if x not in revdir_link_targets ]

        # remove obsolete dirs
        for d in revdir_delete_folders:
            logging.info(" -- remove folder %s" % d)
            if not tryonly:
                rmtree(d)

        for d in sorted([ x for x in glob('%s*' % revdir_target) if not (x in revdir_delete_links or x in revdir_delete_folders)]):
            logging.info(" -- keeping %s" % d)



    def cleanup_images(self, base, image, tryonly):
        image_base = os.path.join(base, image)
        image_stable = "%s-stable.sqfs" % image_base
        image_oldstable = "%s-oldstable.sqfs" % image_base

        keep_stable = self.image_cfg('keep-stable', image=image)
        keep_oldstable = self.image_cfg('keep-oldstable', image=image)
        keep_testing = self.image_cfg('keep-testing', image=image)

        # remove obsolete links
        delete_links = sorted(glob("%s.r*" % image_oldstable),reverse=True)[keep_oldstable:]
        delete_links += sorted(glob("%s.r*" % image_stable),reverse=True)[keep_stable:]
        for l in delete_links:
            logging.info(" -- remove link %s" % l)
            if not tryonly:
                os.unlink(l)

        link_targets = [os.readlink(x) for x in glob("%s.r*" % image_oldstable)]
        link_targets += [os.readlink(x) for x in glob("%s.r*" % image_stable)]

        delete_images = sorted(glob("%s.r*" % image_base),reverse=True)[keep_testing:]
        delete_images = [ x for x in delete_images if x not in link_targets ]

        # remove obsolete dirs
        for i in delete_images:
            logging.info(" -- remove folder %s" % i)
            if not tryonly:
                os.unlink(i)

        for i in sorted([ x for x in glob('%s*' % image_base) if not (x in delete_links or x in delete_images)]):
            logging.info(" -- keeping %s" % i)


    def cleanup(self, image=None, tryonly=False):
        if tryonly:
            logging.info("[TRYONLY]  Cleanup old revisions")
        else:
            logging.info("Cleanup old revisions")
        logging.info(" -- tftpd-path")
        self.cleanup_revdirs(self.cfg('tftpd-path'), self.image_cfg('name', image=image), tryonly)
        logging.info(" -- www-path")
        self.cleanup_revdirs(self.cfg('www-path'), self.image_cfg('name', image=image), tryonly)
        logging.info(" -- image-path")
        self.cleanup_images("%s/sqfs" % self.cfg('image-path'), self.image_cfg('name', image=image), tryonly)




@click.group(chain=True)
@click.pass_context
@click.option('--image', '-i', default=None, help="(optional) image name - otherwise use default from config")
def cli(ctx, image):
    """
        -[ OpenSLX Manager ]-

        This tool will help you with some of the common workflows. For more detailed
        information on each sub-command use:

            'openslx-manager SUBCOMMAND --help'

    """
    ctx.obj['MGR'] = OpenslxManager()
    ctx.obj['IMAGE'] = image


@cli.command()
@click.pass_context
def testing_deploy(ctx):
    """
        Deploy latest build kernel, initrd, stage32 on common shares
    """
    mgr = ctx.obj['MGR']
    mgr.deploy_testing(ctx.obj['IMAGE'])


@cli.command()
@click.pass_context
def sync_and_build(ctx):
    """
        Sync data from image and build initrd and stage32 package
    """
    mgr = ctx.obj['MGR']
    mgr.update_testing(ctx.obj['IMAGE'])


@cli.command()
@click.pass_context
def build_runtime(ctx):
    """
        Build runtime config aka conf.tgz
    """
    mgr = ctx.obj['MGR']
    mgr.update_runtime_config(ctx.obj['IMAGE'])


@cli.command()
@click.pass_context
def build_on_remote(ctx):
    """
        Rebuild modules on reference machine.
    """
    mgr = ctx.obj['MGR']
    mgr.rebuild_remote(ctx.obj['IMAGE'])


@cli.command()
@click.pass_context
def build_filesystem(ctx):
    """
        Update dnbd3 image - this will take long (hours)..
    """
    mgr = ctx.obj['MGR']
    mgr.update_filesystem(ctx.obj['IMAGE'])


@cli.command()
@click.pass_context
def testing_promote(ctx):
    """
        Update stable, old-stable links
    """
    mgr = ctx.obj['MGR']
    mgr.promote_testing(ctx.obj['IMAGE'])


@cli.command()
@click.option('--tryonly', is_flag=True, default=False, help="Don't delete anything - just show what would be done.")
@click.pass_context
def cleanup(ctx, tryonly):
    """
        Remove old builds and configs.
    """
    mgr = ctx.obj['MGR']
    mgr.cleanup(ctx.obj['IMAGE'], tryonly)


@cli.command()
@click.pass_context
def reload_dnbd3(ctx):
    """
        Send SIGUSR1 to all dnbd3servers configured - will enable new images on share.
    """
    mgr = ctx.obj['MGR']
    mgr.reload_dnbd3()


@cli.command()
@click.pass_context
def config_dump(ctx):
    """
        Dump config.
    """
    mgr = ctx.obj['MGR']
    mgr.dump_config()


@cli.command()
@click.pass_context
def config_show_default(ctx):
    """
        Show name of default image config.
    """
    mgr = ctx.obj['MGR']
    mgr.show_default()


if __name__ == '__main__':
    try:
        cli(obj={})
    except Exception as e:
        logging.error("Uncaugt exception: %s" % e)
        traceback.print_stack()
