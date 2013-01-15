#!/usr/bin/env python
# -*- coding: utf-8 -*-

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function
from __future__ import unicode_literals

import logging
import re

from ralph import settings
from ralph.business.models import Venture
from ralph.util import plugin
from ralph.cmdb.integration.lib.fisheye import Fisheye
from ralph.cmdb.integration.lib.puppet_yaml import  load
from ralph.cmdb import models as db
from ralph.cmdb.integration.base import BaseImporter
from ralph.cmdb.integration.util import strip_timezone

from lck.django.common import nested_commit_on_success

logger = logging.getLogger(__name__)


class PuppetAgentsImporter(BaseImporter):
    """Every Puppet host after applying configurations sends yaml textfile report to
    the puppet master server. We hook into this request on the puppet master server
    and parse requests. Only changed hosts are written into the database. """

    def __init__(self):
        pass

    def import_file(self, path):
        contents = open(path, 'r').read()
        self.import_contents(contents)

    def import_contents(self, contents):
        if not contents:
            raise UserWarning('No content to import!')
        yaml = load(contents)
        host = yaml.host
        try:
            status = yaml.status
        except AttributeError:
            logger.warning("Got unknown report status from host %s, skipped!"  % host)
            return

        if status == 'unchanged':
            # skip it, we import only changed/failed
            return
        logger.debug('Got puppet change/failed request:  host: %s kind: %s' % (
                   yaml.host, yaml.kind
                ))
        report = db.CIChangePuppet()
        report.configuration_version = yaml.configuration_version or ''
        report.host = yaml.host
        report.kind = yaml.kind
        report.time = yaml.time
        report.ci = self.get_ci_by_name(host)
        report.status = status
        report.save()
        if settings.PUPPET_SAVE_UNCHANGED_RESOURCES and status !='unchanged':
                for key in yaml.resource_statuses:
                    resource_status = yaml.resource_statuses[key]
                    change_count = resource_status.change_count
                    changed = resource_status.changed
                    type = resource_status.resource_type
                    time = resource_status.time
                    logger.debug('Resource status  %s %s %s %s %s' % (
                       key, change_count, changed, type, time
                    ))
                    obj = db.PuppetResourceStatus()
                    obj.change_count = change_count
                    obj.cichange = report
                    obj.changed = changed
                    obj.failed = getattr(resource_status, 'failed', 0)
                    obj.skipped = getattr(resource_status, 'skipped', 0)
                    obj.file = resource_status.file or ''
                    obj.line = resource_status.line or 0
                    obj.resource = resource_status.resource
                    obj.resource_type = resource_status.resource_type
                    obj.time = resource_status.time
                    obj.title = resource_status.title
                    obj.save()
                    for event in resource_status.events:
                        msg = event.message
                        name = event.name
                        pvalue = event.previous_value
                        dvalue = event.desired_value
                        pr = event.property
                        stat = event.status
                        logger.debug('EVENT %r %r %r %r %r' % (msg, name, pvalue, dvalue, stat)
                        )
        if status == 'changed' or status == 'failed':
            for log in yaml.logs:
                level = log.level
                message = log.message
                time = log.time
                title = status.title
                try:
                    f = log.file
                except:
                    f = ''
                    pass
                dblog = db.PuppetLog()
                dblog.cichange= report
                dblog.source = log.source
                dblog.message = message[:1024]
                dblog.time = time
                dblog.level = level
                dblog.save()
                logger.debug('-' * 40)
                logger.debug('''
title=%s
host=%s
status=%s
level=%s
message=%s
time=%s'''  % (title(), host, status, level, message, time))


class PuppetGitImporter(BaseImporter):
    """ Fetch changesets from fisheye repo.
    CI's are recognized from changed repo directories. Eg:
    ./modules/venture/venture__role
    """
    def __init__(self, fisheye_class=Fisheye):
        self.fisheye = fisheye_class()

    @staticmethod
    @plugin.register(chain='cmdb_git')
    def git(context):
        x = PuppetGitImporter()
        x.import_git()
        return (True, 'Done', context)

    def is_imported(self, changeset):
        return db.CIChangeGit.objects.filter(changeset=changeset).exists()

    @nested_commit_on_success
    def import_changeset(self, changeset):
        x = self.fisheye
        details = x.get_details(changeset)
        logger.debug(details.comment)
        c = db.CIChangeGit()
        c.comment = unicode(details.comment)[0:999]
        c.time = strip_timezone(details.get('date'))
        files_list_str = ""
        try:
            files = details.fileRevisionKey
            files_list = []
            for f in files:
                path = (f.get('path'))
                files_list.append(path)
            files_list_str = '#'.join(files_list)
        except AttributeError:
            files_list_str = ''
            files_list = []
        c.file_paths = unicode(files_list_str)[0:3000]
        c.author = details.get('author')
        c.ci = self.get_ci_by_path(files_list)
        c.changeset = changeset
        c.save()

    def reconcilate(self, ch):
        obj = db.CIChangeGit.objects.get(changeset=ch)
        ci = self.get_ci_by_path(obj.file_paths.split('#'))
        if ci:
            obj.ci = ci
            obj.save()
            ch = db.CIChange.get_by_content_object(obj)
            ch.ci = ci
            ch.save()

    def import_git(self):
        ret = self.fisheye.get_changes()
        for changeset in ret.getchildren():
            if not self.is_imported(changeset):
                self.import_changeset(changeset)
            else:
                self.reconcilate(changeset)

    def find_venture(self, name):
        try:
            return db.CI.get_by_content_object(
                Venture.objects.filter(symbol=name)[0]
            )
        except:
            return None

    def find_role(self, venture_ci, role):
        try:
            roles = [x.child for x in db.CIRelation.objects.filter(parent=venture_ci,
                type=db.CI_RELATION_TYPES.HASROLE.id) if x.child.name == role]
            if roles:
                return roles[0]
        except:
            return None

    def find_ci_by_venturerole(self, role):
        venture = role[0]
        role = role[1]
        v = self.find_venture(venture)
        if not v:
            v = self.find_venture(venture.replace('-', '_'))
            if not v:
                return None
        r = self.find_role(v, role.replace('.pp', ''))
        if not r:
            r = self.find_role(v, role.replace('.pp', '').replace('__', '_'))
            if not r:
                # can't find role. assign none for now.
                return None
            else:
                return r
        else:
            return r

    def get_ci_by_path_mapping(self, path):
        for mapping in db.GitPathMapping.objects.all():
            if mapping.is_regex:
                compiled = re.compile(mapping.path)
                if compiled.match(path):
                    return mapping.ci
            else:
                if path == mapping.path:
                    return mapping.ci

    def get_ci_by_path(self, paths):
        ventures_1 = re.compile('modules/ventures/([^\/]+)/files/(.*)')
        ventures_2 = re.compile('modules/ventures/([^\/]+)/manifests/(.*)')
        for path in paths:
            groups = None
            if ventures_1.match(path):
                groups = ventures_1.match(path).groups()
                ci = self.find_ci_by_venturerole(groups)
                if ci:
                    return ci
            elif ventures_2.match(path):
                groups = ventures_2.match(path).groups()
                ci = self.find_ci_by_venturerole(groups)
                if ci:
                    return ci
            elif self.get_ci_by_path_mapping(self, path):
                pass



