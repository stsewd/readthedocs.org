"""Models for the builds app."""

import datetime
import logging
import os.path
import re
from shutil import rmtree

from django.conf import settings
from django.db import models
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import ugettext
from django.utils.translation import ugettext_lazy as _
from guardian.shortcuts import assign
from jsonfield import JSONField
from taggit.managers import TaggableManager

from readthedocs.core.utils import broadcast
from readthedocs.projects.constants import (
    BITBUCKET_URL,
    GITHUB_URL,
    GITLAB_URL,
    PRIVACY_CHOICES,
    PRIVATE,
)
from readthedocs.projects.models import APIProject, Project

from .constants import (
    BRANCH,
    BUILD_STATE,
    BUILD_STATE_FINISHED,
    BUILD_STATE_TRIGGERED,
    BUILD_TYPES,
    LATEST,
    NON_REPOSITORY_VERSIONS,
    STABLE,
    TAG,
    VERSION_TYPES,
)
from .managers import VersionManager
from .querysets import BuildQuerySet, RelatedBuildQuerySet, VersionQuerySet
from .utils import (
    get_bitbucket_username_repo,
    get_github_username_repo,
    get_gitlab_username_repo,
)
from .version_slug import VersionSlugField


DEFAULT_VERSION_PRIVACY_LEVEL = getattr(
    settings,
    'DEFAULT_VERSION_PRIVACY_LEVEL',
    'public',
)

log = logging.getLogger(__name__)


class Version(models.Model):

    """Version of a ``Project``."""

    project = models.ForeignKey(
        Project,
        verbose_name=_('Project'),
        related_name='versions',
    )
    type = models.CharField(
        _('Type'),
        max_length=20,
        choices=VERSION_TYPES,
        default='unknown',
    )
    # used by the vcs backend

    #: The identifier is the ID for the revision this is version is for. This
    #: might be the revision number (e.g. in SVN), or the commit hash (e.g. in
    #: Git). If the this version is pointing to a branch, then ``identifier``
    #: will contain the branch name.
    identifier = models.CharField(_('Identifier'), max_length=255)

    #: This is the actual name that we got for the commit stored in
    #: ``identifier``. This might be the tag or branch name like ``"v1.0.4"``.
    #: However this might also hold special version names like ``"latest"``
    #: and ``"stable"``.
    verbose_name = models.CharField(_('Verbose Name'), max_length=255)

    #: The slug is the slugified version of ``verbose_name`` that can be used
    #: in the URL to identify this version in a project. It's also used in the
    #: filesystem to determine how the paths for this version are called. It
    #: must not be used for any other identifying purposes.
    slug = VersionSlugField(
        _('Slug'),
        max_length=255,
        populate_from='verbose_name',
    )

    supported = models.BooleanField(_('Supported'), default=True)
    active = models.BooleanField(_('Active'), default=False)
    built = models.BooleanField(_('Built'), default=False)
    uploaded = models.BooleanField(_('Uploaded'), default=False)
    privacy_level = models.CharField(
        _('Privacy Level'),
        max_length=20,
        choices=PRIVACY_CHOICES,
        default=DEFAULT_VERSION_PRIVACY_LEVEL,
        help_text=_('Level of privacy for this Version.'),
    )
    tags = TaggableManager(blank=True)
    machine = models.BooleanField(_('Machine Created'), default=False)

    objects = VersionManager.from_queryset(VersionQuerySet)()

    class Meta:
        unique_together = [('project', 'slug')]
        ordering = ['-verbose_name']
        permissions = (
            # Translators: Permission around whether a user can view the
            #              version
            ('view_version', _('View Version')),
        )

    def __str__(self):
        return ugettext(
            'Version {version} of {project} ({pk})'.format(
                version=self.verbose_name,
                project=self.project,
                pk=self.pk,
            ),
        )

    @property
    def config(self):
        """
        Proxy to the configuration of the build.

        :returns: The configuration used in the last successful build.
        :rtype: dict
        """
        last_build = (
            self.builds.filter(
                state='finished',
                success=True,
            ).order_by('-date').first()
        )
        return last_build.config

    @property
    def commit_name(self):
        """
        Return the branch name, the tag name or the revision identifier.

        The result could be used as ref in a git repo, e.g. for linking to
        GitHub, Bitbucket or GitLab.
        """
        # LATEST is special as it is usually a branch but does not contain the
        # name in verbose_name.
        if self.slug == LATEST:
            if self.project.default_branch:
                return self.project.default_branch
            return self.project.vcs_repo().fallback_branch

        if self.slug == STABLE:
            if self.type == BRANCH:
                # Special case, as we do not store the original branch name
                # that the stable version works on. We can only interpolate the
                # name from the commit identifier, but it's hacky.
                # TODO: Refactor ``Version`` to store more actual info about
                # the underlying commits.
                if self.identifier.startswith('origin/'):
                    return self.identifier[len('origin/'):]
            return self.identifier

        # By now we must have handled all special versions.
        if self.slug in NON_REPOSITORY_VERSIONS:
            raise Exception('All special versions must be handled by now.')

        if self.type in (BRANCH, TAG):
            # If this version is a branch or a tag, the verbose_name will
            # contain the actual name. We cannot use identifier as this might
            # include the "origin/..." part in the case of a branch. A tag
            # would contain the hash in identifier, which is not as pretty as
            # the actual tag name.
            return self.verbose_name

        # If we came that far it's not a special version nor a branch or tag.
        # Therefore just return the identifier to make a safe guess.
        log.debug(
            'TODO: Raise an exception here. Testing what cases it happens',
        )
        return self.identifier

    def get_absolute_url(self):
        if not self.built and not self.uploaded:
            return reverse(
                'project_version_detail',
                kwargs={
                    'project_slug': self.project.slug,
                    'version_slug': self.slug,
                },
            )
        private = self.privacy_level == PRIVATE
        return self.project.get_docs_url(
            version_slug=self.slug,
            private=private,
        )

    def save(self, *args, **kwargs):  # pylint: disable=arguments-differ
        """Add permissions to the Version for all owners on save."""
        from readthedocs.projects import tasks
        obj = super().save(*args, **kwargs)
        for owner in self.project.users.all():
            assign('view_version', owner, self)
        broadcast(
            type='app',
            task=tasks.symlink_project,
            args=[self.project.pk],
        )
        return obj

    def delete(self, *args, **kwargs):  # pylint: disable=arguments-differ
        from readthedocs.projects import tasks
        log.info('Removing files for version %s', self.slug)
        broadcast(
            type='app',
            task=tasks.remove_dirs,
            args=[self.get_artifact_paths()],
        )
        project_pk = self.project.pk
        super().delete(*args, **kwargs)
        broadcast(
            type='app',
            task=tasks.symlink_project,
            args=[project_pk],
        )

    @property
    def identifier_friendly(self):
        """Return display friendly identifier."""
        if re.match(r'^[0-9a-f]{40}$', self.identifier, re.I):
            return self.identifier[:8]
        return self.identifier

    @property
    def is_editable(self):
        return self.type == BRANCH

    def get_subdomain_url(self):
        private = self.privacy_level == PRIVATE
        return self.project.get_docs_url(
            version_slug=self.slug,
            lang_slug=self.project.language,
            private=private,
        )

    def get_downloads(self, pretty=False):
        project = self.project
        data = {}

        def prettify(k):
            return k if pretty else k.lower()

        if project.has_pdf(self.slug):
            data[prettify('PDF')] = project.get_production_media_url(
                'pdf',
                self.slug,
            )
        if project.has_htmlzip(self.slug):
            data[prettify('HTML')] = project.get_production_media_url(
                'htmlzip',
                self.slug,
            )
        if project.has_epub(self.slug):
            data[prettify('Epub')] = project.get_production_media_url(
                'epub',
                self.slug,
            )
        return data

    def get_conf_py_path(self):
        conf_py_path = self.project.conf_dir(self.slug)
        checkout_prefix = self.project.checkout_path(self.slug)
        conf_py_path = os.path.relpath(conf_py_path, checkout_prefix)
        return conf_py_path

    def get_build_path(self):
        """Return version build path if path exists, otherwise `None`."""
        path = self.project.checkout_path(version=self.slug)
        if os.path.exists(path):
            return path
        return None

    def get_artifact_paths(self):
        """
        Return a list of all production artifacts/media path for this version.

        :rtype: list
        """
        paths = []

        for type_ in ('pdf', 'epub', 'htmlzip'):
            paths.append(
                self.project
                .get_production_media_path(type_=type_, version_slug=self.slug),
            )
        paths.append(self.project.rtd_build_path(version=self.slug))

        return paths

    def clean_build_path(self):
        """
        Clean build path for project version.

        Ensure build path is clean for project version. Used to ensure stale
        build checkouts for each project version are removed.
        """
        try:
            path = self.get_build_path()
            if path is not None:
                log.debug('Removing build path %s for %s', path, self)
                rmtree(path)
        except OSError:
            log.exception('Build path cleanup failed')

    def get_github_url(
            self,
            docroot,
            filename,
            source_suffix='.rst',
            action='view',
    ):
        """
        Return a GitHub URL for a given filename.

        :param docroot: Location of documentation in repository
        :param filename: Name of file
        :param source_suffix: File suffix of documentation format
        :param action: `view` (default) or `edit`
        """
        repo_url = self.project.repo
        if 'github' not in repo_url:
            return ''

        if not docroot:
            return ''

        # Normalize /docroot/
        docroot = '/' + docroot.strip('/') + '/'

        if action == 'view':
            action_string = 'blob'
        elif action == 'edit':
            action_string = 'edit'

        user, repo = get_github_username_repo(repo_url)
        if not user and not repo:
            return ''
        repo = repo.rstrip('/')

        if not filename:
            # If there isn't a filename, we don't need a suffix
            source_suffix = ''

        return GITHUB_URL.format(
            user=user,
            repo=repo,
            version=self.commit_name,
            docroot=docroot,
            path=filename,
            source_suffix=source_suffix,
            action=action_string,
        )

    def get_gitlab_url(
            self,
            docroot,
            filename,
            source_suffix='.rst',
            action='view',
    ):
        repo_url = self.project.repo
        if 'gitlab' not in repo_url:
            return ''

        if not docroot:
            return ''

        # Normalize /docroot/
        docroot = '/' + docroot.strip('/') + '/'

        if action == 'view':
            action_string = 'blob'
        elif action == 'edit':
            action_string = 'edit'

        user, repo = get_gitlab_username_repo(repo_url)
        if not user and not repo:
            return ''
        repo = repo.rstrip('/')

        if not filename:
            # If there isn't a filename, we don't need a suffix
            source_suffix = ''

        return GITLAB_URL.format(
            user=user,
            repo=repo,
            version=self.commit_name,
            docroot=docroot,
            path=filename,
            source_suffix=source_suffix,
            action=action_string,
        )

    def get_bitbucket_url(self, docroot, filename, source_suffix='.rst'):
        repo_url = self.project.repo
        if 'bitbucket' not in repo_url:
            return ''
        if not docroot:
            return ''

        # Normalize /docroot/
        docroot = '/' + docroot.strip('/') + '/'

        user, repo = get_bitbucket_username_repo(repo_url)
        if not user and not repo:
            return ''
        repo = repo.rstrip('/')

        if not filename:
            # If there isn't a filename, we don't need a suffix
            source_suffix = ''

        return BITBUCKET_URL.format(
            user=user,
            repo=repo,
            version=self.commit_name,
            docroot=docroot,
            path=filename,
            source_suffix=source_suffix,
        )


class APIVersion(Version):

    """
    Version proxy model for API data deserialization.

    This replaces the pattern where API data was deserialized into a mocked
    :py:class:`Version` object.
    This pattern was confusing, as it was not explicit
    as to what form of object you were working with -- API backed or database
    backed.

    This model preserves the Version model methods, allowing for overrides on
    model field differences. This model pattern will generally only be used on
    builder instances, where we are interacting solely with API data.
    """

    project = None

    class Meta:
        proxy = True

    def __init__(self, *args, **kwargs):
        self.project = APIProject(**kwargs.pop('project', {}))
        # These fields only exist on the API return, not on the model, so we'll
        # remove them to avoid throwing exceptions due to unexpected fields
        for key in ['resource_uri', 'absolute_url', 'downloads']:
            try:
                del kwargs[key]
            except KeyError:
                pass
        super().__init__(*args, **kwargs)

    def save(self, *args, **kwargs):
        return 0


class Build(models.Model):

    """Build data."""

    project = models.ForeignKey(
        Project,
        verbose_name=_('Project'),
        related_name='builds',
    )
    version = models.ForeignKey(
        Version,
        verbose_name=_('Version'),
        null=True,
        related_name='builds',
    )
    type = models.CharField(
        _('Type'),
        max_length=55,
        choices=BUILD_TYPES,
        default='html',
    )
    state = models.CharField(
        _('State'),
        max_length=55,
        choices=BUILD_STATE,
        default='finished',
    )
    date = models.DateTimeField(_('Date'), auto_now_add=True)
    success = models.BooleanField(_('Success'), default=True)

    setup = models.TextField(_('Setup'), null=True, blank=True)
    setup_error = models.TextField(_('Setup error'), null=True, blank=True)
    output = models.TextField(_('Output'), default='', blank=True)
    error = models.TextField(_('Error'), default='', blank=True)
    exit_code = models.IntegerField(_('Exit code'), null=True, blank=True)
    commit = models.CharField(
        _('Commit'),
        max_length=255,
        null=True,
        blank=True,
    )
    _config = JSONField(_('Configuration used in the build'), default=dict)

    length = models.IntegerField(_('Build Length'), null=True, blank=True)

    builder = models.CharField(
        _('Builder'),
        max_length=255,
        null=True,
        blank=True,
    )

    cold_storage = models.NullBooleanField(
        _('Cold Storage'),
        help_text='Build steps stored outside the database.',
    )

    # Manager

    objects = BuildQuerySet.as_manager()

    CONFIG_KEY = '__config'

    class Meta:
        ordering = ['-date']
        get_latest_by = 'date'
        index_together = [['version', 'state', 'type']]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._config_changed = False

    @property
    def previous(self):
        """
        Returns the previous build to the current one.

        Matching the project and version.
        """
        date = self.date or timezone.now()
        if self.project is not None and self.version is not None:
            return (
                Build.objects.filter(
                    project=self.project,
                    version=self.version,
                    date__lt=date,
                ).order_by('-date').first()
            )
        return None

    @property
    def config(self):
        """
        Get the config used for this build.

        Since we are saving the config into the JSON field only when it differs
        from the previous one, this helper returns the correct JSON used in this
        Build object (it could be stored in this object or one of the previous
        ones).
        """
        if self.CONFIG_KEY in self._config:
            return Build.objects.get(pk=self._config[self.CONFIG_KEY])._config
        return self._config

    @config.setter
    def config(self, value):
        """
        Set `_config` to value.

        `_config` should never be set directly from outside the class.
        """
        self._config = value
        self._config_changed = True

    def save(self, *args, **kwargs):  # noqa
        """
        Save object.

        To save space on the db we only save the config if it's different
        from the previous one.

        If the config is the same, we save the pk of the object
        that has the **real** config under the `CONFIG_KEY` key.
        """
        if self.pk is None or self._config_changed:
            previous = self.previous
            # yapf: disable
            if (
                previous is not None and self._config and
                self._config == previous.config
            ):
                # yapf: enable
                previous_pk = previous._config.get(self.CONFIG_KEY, previous.pk)
                self._config = {self.CONFIG_KEY: previous_pk}
        super().save(*args, **kwargs)
        self._config_changed = False

    def __str__(self):
        return ugettext(
            'Build {project} for {usernames} ({pk})'.format(
                project=self.project,
                usernames=' '.join(
                    self.project.users.all().values_list('username', flat=True),
                ),
                pk=self.pk,
            ),
        )

    def get_absolute_url(self):
        return reverse('builds_detail', args=[self.project.slug, self.pk])

    @property
    def finished(self):
        """Return if build has a finished state."""
        return self.state == BUILD_STATE_FINISHED

    @property
    def is_stale(self):
        """Return if build state is triggered & date more than 5m ago."""
        mins_ago = timezone.now() - datetime.timedelta(minutes=5)
        return self.state == BUILD_STATE_TRIGGERED and self.date < mins_ago


class BuildCommandResultMixin:

    """
    Mixin for common command result methods/properties.

    Shared methods between the database model :py:class:`BuildCommandResult` and
    non-model respresentations of build command results from the API
    """

    @property
    def successful(self):
        """Did the command exit with a successful exit code."""
        return self.exit_code == 0

    @property
    def failed(self):
        """
        Did the command exit with a failing exit code.

        Helper for inverse of :py:meth:`successful`
        """
        return not self.successful


class BuildCommandResult(BuildCommandResultMixin, models.Model):

    """Build command for a ``Build``."""

    build = models.ForeignKey(
        Build,
        verbose_name=_('Build'),
        related_name='commands',
    )

    command = models.TextField(_('Command'))
    description = models.TextField(_('Description'), blank=True)
    output = models.TextField(_('Command output'), blank=True)
    exit_code = models.IntegerField(_('Command exit code'))

    start_time = models.DateTimeField(_('Start time'))
    end_time = models.DateTimeField(_('End time'))

    class Meta:
        ordering = ['start_time']
        get_latest_by = 'start_time'

    objects = RelatedBuildQuerySet.as_manager()

    def __str__(self):
        return (
            ugettext('Build command {pk} for build {build}')
            .format(pk=self.pk, build=self.build)
        )

    @property
    def run_time(self):
        """Total command runtime in seconds."""
        if self.start_time is not None and self.end_time is not None:
            diff = self.end_time - self.start_time
            return diff.seconds
