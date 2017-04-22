# coding=utf-8
import json
import re
import os
import logging
import socket
import traceback
import time
import operator
import requests

from collections import defaultdict
from bs4 import UnicodeDammit
from babelfish import LanguageReverseError
from guessit.jsonutils import GuessitEncoder
from subliminal import ProviderError, refiner_manager

from subliminal.score import compute_score as default_compute_score
from subliminal.subtitle import SUBTITLE_EXTENSIONS
from subliminal.utils import hash_napiprojekt, hash_opensubtitles, hash_shooter, hash_thesubdb
from subliminal.video import VIDEO_EXTENSIONS, Video, Episode, Movie
from subliminal.core import guessit, Language, ProviderPool, io

logger = logging.getLogger(__name__)

# may be absolute or relative paths; set to selected options
CUSTOM_PATHS = []
INCLUDE_EXOTIC_SUBS = True

DOWNLOAD_TRIES = 0
DOWNLOAD_RETRY_SLEEP = 2

REMOVE_CRAP_FROM_FILENAME = re.compile("(?i)[_-](obfuscated|scrambled)(\.[\w]+)$")


class PatchedProviderPool(ProviderPool):
    def list_subtitles(self, video, languages):
        """List subtitles.
        
        patch: handle LanguageReverseError

        :param video: video to list subtitles for.
        :type video: :class:`~subliminal.video.Video`
        :param languages: languages to search for.
        :type languages: set of :class:`~babelfish.language.Language`
        :return: found subtitles.
        :rtype: list of :class:`~subliminal.subtitle.Subtitle`

        """
        subtitles = []

        for name in self.providers:
            # check discarded providers
            if name in self.discarded_providers:
                logger.debug('Skipping discarded provider %r', name)
                continue

            # list subtitles
            try:
                provider_subtitles = self.list_subtitles_provider(name, video, languages)
            except LanguageReverseError:
                logger.exception("Unexpected language reverse error in %s, skipping. Error: %s", name,
                                 traceback.format_exc())
                continue

            if provider_subtitles is None:
                logger.info('Discarding provider %s', name)
                self.discarded_providers.add(name)
                continue

            # add the subtitles
            subtitles.extend(provider_subtitles)

        return subtitles

    def download_subtitle(self, subtitle):
        """Download `subtitle`'s :attr:`~subliminal.subtitle.Subtitle.content`.
        
        patch: add retry functionality
        
        :param subtitle: subtitle to download.
        :type subtitle: :class:`~subliminal.subtitle.Subtitle`
        :return: `True` if the subtitle has been successfully downloaded, `False` otherwise.
        :rtype: bool
        """
        # check discarded providers
        if subtitle.provider_name in self.discarded_providers:
            logger.warning('Provider %r is discarded', subtitle.provider_name)
            return False

        logger.info('Downloading subtitle %r', subtitle)
        tries = 0

        # retry downloading on failure until settings' download retry limit hit
        while True:
            tries += 1
            try:
                self[subtitle.provider_name].download_subtitle(subtitle)
                break
            except (requests.Timeout, socket.timeout):
                logger.error('Provider %r timed out', subtitle.provider_name)
            except ProviderError:
                logger.error('Unexpected error in provider %r, Traceback: %s', subtitle.provider_name,
                             traceback.format_exc())
            except:
                logger.exception('Unexpected error in provider %r, Traceback: %s', subtitle.provider_name,
                                 traceback.format_exc())

            if tries == DOWNLOAD_TRIES:
                self.discarded_providers.add(subtitle.provider_name)
                logger.error('Maximum retries reached for provider %r, discarding it', subtitle.provider_name)
                return False

            # don't hammer the provider
            logger.debug('Errors while downloading subtitle, retrying provider %r in %s seconds',
                         subtitle.provider_name, DOWNLOAD_RETRY_SLEEP)
            time.sleep(DOWNLOAD_RETRY_SLEEP)

        # check subtitle validity
        if not subtitle.is_valid():
            logger.error('Invalid subtitle')
            return False

        return True

    def download_best_subtitles(self, subtitles, video, languages, min_score=0, hearing_impaired=False, only_one=False,
                                compute_score=None):
        """Download the best matching subtitles.
        
        patch: 
            - hearing_impaired is now string
            - add .score to subtitle
            - move all languages check further to the top (still necessary?)

        :param subtitles: the subtitles to use.
        :type subtitles: list of :class:`~subliminal.subtitle.Subtitle`
        :param video: video to download subtitles for.
        :type video: :class:`~subliminal.video.Video`
        :param languages: languages to download.
        :type languages: set of :class:`~babelfish.language.Language`
        :param int min_score: minimum score for a subtitle to be downloaded.
        :param bool hearing_impaired: hearing impaired preference.
        :param bool only_one: download only one subtitle, not one per language.
        :param compute_score: function that takes `subtitle` and `video` as positional arguments,
            `hearing_impaired` as keyword argument and returns the score.
        :return: downloaded subtitles.
        :rtype: list of :class:`~subliminal.subtitle.Subtitle`

        """
        compute_score = compute_score or default_compute_score
        use_hearing_impaired = hearing_impaired in ("prefer", "force HI")

        # sort subtitles by score
        unsorted_subtitles = []

        for s in subtitles:
            # get the matches
            try:
                matches = s.get_matches(video)
            except AttributeError:
                logger.error("Match computation failed for %s: %s", s, traceback.format_exc())
                continue

            logger.debug('Found matches %r', matches)
            unsorted_subtitles.append(
                (s, compute_score(matches, s, video, hearing_impaired=use_hearing_impaired), matches))

        # sort subtitles by score
        scored_subtitles = sorted(unsorted_subtitles, key=operator.itemgetter(1), reverse=True)

        # download best subtitles, falling back on the next on error
        downloaded_subtitles = []
        for subtitle, score, matches in scored_subtitles:
            # check score
            if score < min_score:
                logger.info('Score %d is below min_score (%d)', score, min_score)
                break

            # stop when all languages are downloaded
            if set(s.language for s in downloaded_subtitles) == languages:
                logger.debug('All languages downloaded')
                break

            # check downloaded languages
            if subtitle.language in set(s.language for s in downloaded_subtitles):
                logger.debug('Skipping subtitle: %r already downloaded', subtitle.language)
                continue

            # bail out if hearing_impaired was wrong
            if "hearing_impaired" not in matches and hearing_impaired in ("force HI", "force non-HI"):
                logger.debug('Skipping subtitle: %r with score %d because hearing-impaired set to %s', subtitle,
                             score, hearing_impaired)
                continue

            # download
            logger.debug("Trying to download subtitle %r with matches %s, score: %s; release(s): %s", subtitle, matches,
                         score, subtitle.release_info)
            if self.download_subtitle(subtitle):
                subtitle.score = score
                downloaded_subtitles.append(subtitle)

            # stop if only one subtitle is requested
            if only_one:
                logger.debug('Only one subtitle downloaded')
                break

        return downloaded_subtitles


def scan_video(path, dont_use_actual_file=False, hints=None):
    """Scan a video from a `path`.

    patch:
        - allow passing of hints/options to guessit
        - allow dry-run with dont_use_actual_file
        - add crap removal (obfuscated/scrambled)
        - trust plex's movie name

    :param str path: existing path to the video.
    :return: the scanned video.
    :rtype: :class:`~subliminal.video.Video`

    """
    video_type = hints.get("type")

    # check for non-existing path
    if not dont_use_actual_file and not os.path.exists(path):
        raise ValueError('Path does not exist')

    # check video extension
    if not path.endswith(VIDEO_EXTENSIONS):
        raise ValueError('%r is not a valid video extension' % os.path.splitext(path)[1])

    dirpath, filename = os.path.split(path)
    logger.info('Scanning video %r in %r', filename, dirpath)

    # hint guessit the filename itself and its 2 parent directories if we're an episode (most likely
    # Series name/Season/filename), else only one
    guess_from = os.path.join(*os.path.normpath(path).split(os.path.sep)[-3 if video_type == "episode" else -2:])
    guess_from = REMOVE_CRAP_FROM_FILENAME.sub(r"\2", guess_from)

    # guess
    guessed_result = guessit(guess_from, options=hints or {})
    logger.debug('GuessIt found: %s', json.dumps(guessed_result, cls=GuessitEncoder, indent=4, ensure_ascii=False))
    video = Video.fromguess(path, guessed_result)

    # trust plex's movie name
    if video_type == "movie" and hints.get("expected_title"):
        video.title = hints.get("expected_title")[0]

    if dont_use_actual_file:
        return video

    # size and hashes
    video.size = os.path.getsize(path)
    if video.size > 10485760:
        logger.debug('Size is %d', video.size)
        video.hashes['opensubtitles'] = hash_opensubtitles(path)
        video.hashes['shooter'] = hash_shooter(path)
        video.hashes['thesubdb'] = hash_thesubdb(path)
        video.hashes['napiprojekt'] = hash_napiprojekt(path)
        logger.debug('Computed hashes %r', video.hashes)
    else:
        logger.warning('Size is lower than 10MB: hashes not computed')

    return video


def _search_external_subtitles(path, forced_tag=False):
    dirpath, filename = os.path.split(path)
    dirpath = dirpath or '.'
    fileroot, fileext = os.path.splitext(filename)
    subtitles = {}
    for p in os.listdir(dirpath):
        # keep only valid subtitle filenames
        if not p.startswith(fileroot) or not p.endswith(SUBTITLE_EXTENSIONS):
            continue

        p_root, p_ext = os.path.splitext(p)
        if not INCLUDE_EXOTIC_SUBS and p_ext not in (".srt", ".ass", ".ssa"):
            continue

        # extract potential forced/normal/default tag
        # fixme: duplicate from subtitlehelpers
        split_tag = p_root.rsplit('.', 1)
        adv_tag = None
        if len(split_tag) > 1:
            adv_tag = split_tag[1].lower()
            if adv_tag in ['forced', 'normal', 'default']:
                p_root = split_tag[0]

        # forced wanted but NIL
        if forced_tag and adv_tag != "forced":
            continue

        # extract the potential language code
        language_code = p_root[len(fileroot):].replace('_', '-')[1:]

        # default language is undefined
        language = Language('und')

        # attempt to parse
        if language_code:
            try:
                language = Language.fromietf(language_code)
            except ValueError:
                logger.error('Cannot parse language code %r', language_code)

        subtitles[p] = language

    logger.debug('Found subtitles %r', subtitles)

    return subtitles


def search_external_subtitles(path, forced_tag=False):
    """
    wrap original search_external_subtitles function to search multiple paths for one given video
    # todo: cleanup and merge with _search_external_subtitles
    """
    video_path, video_filename = os.path.split(path)
    subtitles = {}
    for folder_or_subfolder in [video_path] + CUSTOM_PATHS:
        # folder_or_subfolder may be a relative path or an absolute one
        try:
            abspath = unicode(os.path.abspath(
                os.path.join(*[video_path if not os.path.isabs(folder_or_subfolder) else "", folder_or_subfolder,
                               video_filename])))
        except Exception, e:
            logger.error("skipping path %s because of %s", repr(folder_or_subfolder), e)
            continue
        logger.debug("external subs: scanning path %s", abspath)

        if os.path.isdir(os.path.dirname(abspath)):
            subtitles.update(_search_external_subtitles(abspath, forced_tag=forced_tag))
    logger.debug("external subs: found %s", subtitles)
    return subtitles


def list_all_subtitles(videos, languages, **kwargs):
    """List all available subtitles.
    
    patch: remove video check, it has been done before

    The `videos` must pass the `languages` check of :func:`check_video`.

    All other parameters are passed onwards to the :class:`ProviderPool` constructor.

    :param videos: videos to list subtitles for.
    :type videos: set of :class:`~subliminal.video.Video`
    :param languages: languages to search for.
    :type languages: set of :class:`~babelfish.language.Language`
    :return: found subtitles per video.
    :rtype: dict of :class:`~subliminal.video.Video` to list of :class:`~subliminal.subtitle.Subtitle`

    """
    listed_subtitles = defaultdict(list)

    # return immediatly if no video passed the checks
    if not videos:
        return listed_subtitles

    # list subtitles
    with PatchedProviderPool(**kwargs) as pool:
        for video in videos:
            logger.info('Listing subtitles for %r', video)
            subtitles = pool.list_subtitles(video, languages - video.subtitle_languages)
            listed_subtitles[video].extend(subtitles)
            logger.info('Found %d subtitle(s)', len(subtitles))

    return listed_subtitles


def download_subtitles(subtitles, pool_class=ProviderPool, **kwargs):
    """Download :attr:`~subliminal.subtitle.Subtitle.content` of `subtitles`.

    :param subtitles: subtitles to download.
    :type subtitles: list of :class:`~subliminal.subtitle.Subtitle`
    :param pool_class: class to use as provider pool.
    :type pool_class: :class:`ProviderPool`, :class:`AsyncProviderPool` or similar
    :param \*\*kwargs: additional parameters for the provided `pool_class` constructor.

    """
    with pool_class(**kwargs) as pool:
        for subtitle in subtitles:
            logger.info('Downloading subtitle %r with score %s', subtitle, subtitle.score)
            pool.download_subtitle(subtitle)


def get_subtitle_path(video_path, language=None, extension='.srt', forced_tag=False):
    """Get the subtitle path using the `video_path` and `language`.

    :param str video_path: path to the video.
    :param language: language of the subtitle to put in the path.
    :type language: :class:`~babelfish.language.Language`
    :param str extension: extension of the subtitle.
    :return: path of the subtitle.
    :rtype: str

    """
    subtitle_root = os.path.splitext(video_path)[0]

    if language:
        subtitle_root += '.' + str(language)

    if forced_tag:
        subtitle_root += ".forced"

    return subtitle_root + extension


def save_subtitles(video, subtitles, single=False, directory=None, encoding=None, encode_with=None, chmod=None,
                   forced_tag=False, path_decoder=None):
    """Save subtitles on filesystem.

    Subtitles are saved in the order of the list. If a subtitle with a language has already been saved, other subtitles
    with the same language are silently ignored.

    The extension used is `.lang.srt` by default or `.srt` is `single` is `True`, with `lang` being the IETF code for
    the :attr:`~subliminal.subtitle.Subtitle.language` of the subtitle.

    :param video: video of the subtitles.
    :type video: :class:`~subliminal.video.Video`
    :param subtitles: subtitles to save.
    :type subtitles: list of :class:`~subliminal.subtitle.Subtitle`
    :param bool single: save a single subtitle, default is to save one subtitle per language.
    :param str directory: path to directory where to save the subtitles, default is next to the video.
    :param str encoding: encoding in which to save the subtitles, default is to keep original encoding.
    :return: the saved subtitles
    :rtype: list of :class:`~subliminal.subtitle.Subtitle`

    patch: unicode path problems
    """
    saved_subtitles = []
    for subtitle in subtitles:
        # check content
        if subtitle.content is None:
            logger.error('Skipping subtitle %r: no content', subtitle)
            continue

        # check language
        if subtitle.language in set(s.language for s in saved_subtitles):
            logger.debug('Skipping subtitle %r: language already saved', subtitle)
            continue

        # create subtitle path
        subtitle_path = get_subtitle_path(video.name, None if single else subtitle.language, forced_tag=forced_tag)
        if directory is not None:
            subtitle_path = os.path.join(directory, os.path.split(subtitle_path)[1])

        if path_decoder:
            subtitle_path = path_decoder(subtitle_path)

        # force unicode
        subtitle_path = UnicodeDammit(subtitle_path).unicode_markup

        subtitle.storage_path = subtitle_path

        # save content as is or in the specified encoding
        logger.info('Saving %r to %r', subtitle, subtitle_path)
        has_encoder = callable(encode_with)

        if has_encoder:
            logger.info('Using encoder %s' % encode_with.__name__)

        # save normalized subtitle if encoder or no encoding is given
        if has_encoder or encoding is None:
            content = encode_with(subtitle.text) if has_encoder else subtitle.content
            with io.open(subtitle_path, 'wb') as f:
                f.write(content)

            # change chmod if requested
            if chmod:
                os.chmod(subtitle_path, chmod)

            if single:
                break
            continue

        # save subtitle if encoding given
        if encoding is not None:
            with io.open(subtitle_path, 'w', encoding=encoding) as f:
                f.write(subtitle.text)

        # change chmod if requested
        if chmod:
            os.chmod(subtitle_path, chmod)

        saved_subtitles.append(subtitle)

        # check single
        if single:
            break

    return saved_subtitles


def refine(video, episode_refiners=None, movie_refiners=None, **kwargs):
    """Refine a video using :ref:`refiners`.
    
    patch: add traceback logging

    .. note::

        Exceptions raised in refiners are silently passed and logged.

    :param video: the video to refine.
    :type video: :class:`~subliminal.video.Video`
    :param tuple episode_refiners: refiners to use for episodes.
    :param tuple movie_refiners: refiners to use for movies.
    :param \*\*kwargs: additional parameters for the :func:`~subliminal.refiners.refine` functions.

    """
    refiners = ()
    if isinstance(video, Episode):
        refiners = episode_refiners or ('metadata', 'tvdb', 'omdb')
    elif isinstance(video, Movie):
        refiners = movie_refiners or ('metadata', 'omdb')
    for refiner in refiners:
        logger.info('Refining video with %s', refiner)
        try:
            refiner_manager[refiner].plugin(video, **kwargs)
        except:
            logger.exception('Failed to refine video: %s' % traceback.format_exc())
