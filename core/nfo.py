import os
import tempfile
import xml.etree.ElementTree as ET
from typing import Dict, Optional
from xml.dom import minidom


def _ensure_unicode(s):
    return str(s) if s is not None else ''


def generate_movie_nfo(meta: Dict) -> ET.Element:
    movie = ET.Element('movie')
    # title and original
    title_el = ET.SubElement(movie, 'title')
    title_text = _ensure_unicode(meta.get('title') or '')
    title_text = f"{title_text} [REQUEST]" if title_text else '[REQUEST]'
    title_el.text = title_text
    original_el = ET.SubElement(movie, 'originaltitle')
    original_el.text = _ensure_unicode(meta.get('originaltitle') or meta.get('title') or '')
    # sorttitle
    if meta.get('sorttitle'):
        sort = ET.SubElement(movie, 'sorttitle')
        sort.text = _ensure_unicode(meta.get('sorttitle'))
    # Ratings block (radarr-like)
    ratings_block = ET.SubElement(movie, 'ratings')
    # IMDb rating
    if meta.get('imdb_rating') or meta.get('imdb_votes'):
        r_imdb = ET.SubElement(ratings_block, 'rating')
        r_imdb.set('name', 'imdb')
        r_imdb.set('max', '10')
        r_imdb.set('default', 'true')
        if meta.get('imdb_rating'):
            val = ET.SubElement(r_imdb, 'value')
            val.text = _ensure_unicode(meta.get('imdb_rating'))
        if meta.get('imdb_votes'):
            v = ET.SubElement(r_imdb, 'votes')
            v.text = _ensure_unicode(meta.get('imdb_votes'))
    # TheMovieDB rating
    if meta.get('tmdb_rating') or meta.get('tmdb_votes'):
        r_tmdb = ET.SubElement(ratings_block, 'rating')
        r_tmdb.set('name', 'themoviedb')
        r_tmdb.set('max', '10')
        if meta.get('tmdb_rating'):
            val = ET.SubElement(r_tmdb, 'value')
            val.text = _ensure_unicode(meta.get('tmdb_rating'))
        if meta.get('tmdb_votes'):
            v = ET.SubElement(r_tmdb, 'votes')
            v.text = _ensure_unicode(meta.get('tmdb_votes'))
    # Critic/Tomato ratings
    if meta.get('criticrating'):
        r_crit = ET.SubElement(ratings_block, 'rating')
        r_crit.set('name', 'tomatometerallcritics')
        r_crit.set('max', '100')
        val = ET.SubElement(r_crit, 'value')
        val.text = _ensure_unicode(meta.get('criticrating'))
    # Top-level simple rating fields
    if meta.get('tmdb_rating'):
        rating = ET.SubElement(movie, 'rating')
        rating.text = _ensure_unicode(meta.get('tmdb_rating'))
    if meta.get('criticrating'):
        criticrating = ET.SubElement(movie, 'criticrating')
        criticrating.text = _ensure_unicode(meta.get('criticrating'))
    # placeholders for user-visible fields
    ET.SubElement(movie, 'userrating')
    ET.SubElement(movie, 'top250')
    ET.SubElement(movie, 'outline')
    # Plot and tagline
    plot_el = ET.SubElement(movie, 'plot')
    plot_text = _ensure_unicode(meta.get('plot') or '')
    plot_el.text = f"[REQUEST] {plot_text}" if plot_text else '[REQUEST]'
    tagline_el = ET.SubElement(movie, 'tagline')
    tagline_el.text = _ensure_unicode(meta.get('tagline') or '')
    if meta.get('runtime'):
        runtime = ET.SubElement(movie, 'runtime')
        runtime.text = _ensure_unicode(meta.get('runtime'))
    # genres as repeated <tag> or <genre>
    genres = meta.get('genres')
    if genres:
        if isinstance(genres, (list, tuple)):
            for g in genres:
                ge = ET.SubElement(movie, 'genre')
                ge.text = _ensure_unicode(g)
        else:
            for g in str(genres).split(','):
                ge = ET.SubElement(movie, 'genre')
                ge.text = _ensure_unicode(g.strip())
    # ids
    if meta.get('tmdb_id'):
        t = ET.SubElement(movie, 'tmdbid')
        t.text = _ensure_unicode(meta.get('tmdb_id'))
    if meta.get('imdb_id'):
        im = ET.SubElement(movie, 'imdbid')
        im.text = _ensure_unicode(meta.get('imdb_id'))
    # also include uniqueid entries for compatibility
    if meta.get('tmdb_id'):
        uid = ET.SubElement(movie, 'uniqueid')
        uid.set('type', 'tmdb')
        uid.text = _ensure_unicode(meta.get('tmdb_id'))
    if meta.get('imdb_id'):
        uid = ET.SubElement(movie, 'uniqueid')
        uid.set('type', 'imdb')
        uid.text = _ensure_unicode(meta.get('imdb_id'))
    # poster/thumb URL
    # thumb with attributes and fanart
    if meta.get('poster_url'):
        th = ET.SubElement(movie, 'thumb')
        try:
            th.set('aspect', 'poster')
            th.set('preview', _ensure_unicode(meta.get('poster_url')))
        except Exception:
            pass
        th.text = _ensure_unicode(meta.get('poster_url'))
    if meta.get('fanart_url'):
        fan = ET.SubElement(movie, 'fanart')
        fthumb = ET.SubElement(fan, 'thumb')
        try:
            fthumb.set('preview', _ensure_unicode(meta.get('fanart_url')))
        except Exception:
            pass
        fthumb.text = _ensure_unicode(meta.get('fanart_url'))
    # playcount/lastplayed placeholders
    ET.SubElement(movie, 'playcount')
    ET.SubElement(movie, 'lastplayed')
    # ids/uniqueid
    if meta.get('tmdb_id'):
        id_el = ET.SubElement(movie, 'id')
        id_el.text = _ensure_unicode(meta.get('tmdb_id'))
    if meta.get('tmdb_id'):
        uid = ET.SubElement(movie, 'uniqueid')
        uid.set('type', 'tmdb')
        uid.set('default', 'true')
        uid.text = _ensure_unicode(meta.get('tmdb_id'))
    if meta.get('imdb_id'):
        uid = ET.SubElement(movie, 'uniqueid')
        uid.set('type', 'imdb')
        uid.text = _ensure_unicode(meta.get('imdb_id'))
    # genres and basic metadata
    genres = meta.get('genres')
    if genres:
        if isinstance(genres, (list, tuple)):
            for g in genres:
                ge = ET.SubElement(movie, 'genre')
                ge.text = _ensure_unicode(g)
        else:
            for g in str(genres).split(','):
                ge = ET.SubElement(movie, 'genre')
                ge.text = _ensure_unicode(g.strip())
    if meta.get('country'):
        country = ET.SubElement(movie, 'country')
        country.text = _ensure_unicode(meta.get('country'))
    if meta.get('status'):
        status = ET.SubElement(movie, 'status')
        status.text = _ensure_unicode(meta.get('status'))
    if meta.get('credits'):
        credits_el = ET.SubElement(movie, 'credits')
        credits_el.text = _ensure_unicode(meta.get('credits'))
    if meta.get('director'):
        director_el = ET.SubElement(movie, 'director')
        director_el.text = _ensure_unicode(meta.get('director'))
    if meta.get('premiered'):
        prem = ET.SubElement(movie, 'premiered')
        prem.text = _ensure_unicode(meta.get('premiered'))
    if meta.get('year'):
        y = ET.SubElement(movie, 'year')
        y.text = _ensure_unicode(meta.get('year'))
    if meta.get('studio'):
        studio = ET.SubElement(movie, 'studio')
        studio.text = _ensure_unicode(meta.get('studio'))
    if meta.get('trailer'):
        trailer_el = ET.SubElement(movie, 'trailer')
        trailer_el.text = _ensure_unicode(meta.get('trailer'))
    if meta.get('watched') is not None:
        watched_el = ET.SubElement(movie, 'watched')
        watched_el.text = _ensure_unicode(meta.get('watched'))
    # fileinfo/streamdetails if provided
    if meta.get('streamdetails') or meta.get('fileinfo'):
        fi = ET.SubElement(movie, 'fileinfo')
        sd = ET.SubElement(fi, 'streamdetails')
        sd_src = meta.get('streamdetails') or (meta.get('fileinfo', {}).get('streamdetails') if isinstance(meta.get('fileinfo'), dict) else None)
        if sd_src and isinstance(sd_src, dict):
            # video
            vid = sd_src.get('video') or {}
            if vid:
                v_el = ET.SubElement(sd, 'video')
                for k, v in vid.items():
                    e = ET.SubElement(v_el, k)
                    e.text = _ensure_unicode(v)
            # audio
            aud = sd_src.get('audio') or {}
            if aud:
                a_el = ET.SubElement(sd, 'audio')
                for k, v in aud.items():
                    e = ET.SubElement(a_el, k)
                    e.text = _ensure_unicode(v)
            # subtitles (list)
            subs = sd_src.get('subtitle') or sd_src.get('subtitles') or []
            if subs and isinstance(subs, (list, tuple)):
                for s in subs:
                    s_el = ET.SubElement(sd, 'subtitle')
                    lang = s if isinstance(s, str) else s.get('language')
                    if lang:
                        l = ET.SubElement(s_el, 'language')
                        l.text = _ensure_unicode(lang)
    # actors
    actors = meta.get('actors')
    if actors:
        try:
            if isinstance(actors, (list, tuple)):
                for idx, a in enumerate(actors):
                    actor_el = ET.SubElement(movie, 'actor')
                    name = a.get('name') if isinstance(a, dict) else a
                    role = a.get('role') if isinstance(a, dict) else None
                    order = a.get('order') if isinstance(a, dict) else idx
                    name_el = ET.SubElement(actor_el, 'name')
                    name_el.text = _ensure_unicode(name)
                    if role:
                        role_el = ET.SubElement(actor_el, 'role')
                        role_el.text = _ensure_unicode(role)
                    order_el = ET.SubElement(actor_el, 'order')
                    order_el.text = _ensure_unicode(order)
                    thumb = a.get('thumb') if isinstance(a, dict) else None
                    if thumb:
                        t_el = ET.SubElement(actor_el, 'thumb')
                        t_el.text = _ensure_unicode(thumb)
        except Exception:
            pass
    return movie
    return movie


def generate_episode_nfo(meta: Dict) -> ET.Element:
    episode = ET.Element('episodedetails')
    title_el = ET.SubElement(episode, 'title')
    # Always mark episode placeholders as requests
    title_text = _ensure_unicode(meta.get('title') or '')
    title_text = f"{title_text} [REQUEST]" if title_text else '[REQUEST]'
    title_el.text = title_text
    show_el = ET.SubElement(episode, 'showtitle')
    show_el.text = _ensure_unicode(meta.get('showtitle') or '')
    if meta.get('season') is not None:
        s = ET.SubElement(episode, 'season')
        s.text = _ensure_unicode(meta.get('season'))
    if meta.get('episode') is not None:
        e = ET.SubElement(episode, 'episode')
        e.text = _ensure_unicode(meta.get('episode'))
    if meta.get('aired'):
        aired = ET.SubElement(episode, 'aired')
        aired.text = _ensure_unicode(meta.get('aired'))
    # Always include plot and mark as request
    plot = ET.SubElement(episode, 'plot')
    plot_text = _ensure_unicode(meta.get('plot') or '')
    plot.text = f"[REQUEST] {plot_text}" if plot_text else '[REQUEST]'
    if meta.get('tvdb_id'):
        tvdb = ET.SubElement(episode, 'tvdbid')
        tvdb.text = _ensure_unicode(meta.get('tvdb_id'))
    if meta.get('tmdb_id'):
        tmdb = ET.SubElement(episode, 'tmdbid')
        tmdb.text = _ensure_unicode(meta.get('tmdb_id'))
    if meta.get('imdb_id'):
        imdb = ET.SubElement(episode, 'imdbid')
        imdb.text = _ensure_unicode(meta.get('imdb_id'))
    # uniqueid compatibility
    if meta.get('tmdb_id'):
        uid = ET.SubElement(episode, 'uniqueid')
        uid.set('type', 'tmdb')
        uid.text = _ensure_unicode(meta.get('tmdb_id'))
    if meta.get('imdb_id'):
        uid = ET.SubElement(episode, 'uniqueid')
        uid.set('type', 'imdb')
        uid.text = _ensure_unicode(meta.get('imdb_id'))
    if meta.get('poster_url'):
        th = ET.SubElement(episode, 'thumb')
        th.text = _ensure_unicode(meta.get('poster_url'))
    # genres
    genres = meta.get('genres')
    if genres:
        if isinstance(genres, (list, tuple)):
            for g in genres:
                ge = ET.SubElement(episode, 'genre')
                ge.text = _ensure_unicode(g)
        else:
            for g in str(genres).split(','):
                ge = ET.SubElement(episode, 'genre')
                ge.text = _ensure_unicode(g.strip())
    # actors/director/credits
    actors = meta.get('actors')
    if actors:
        try:
            actors_el = ET.SubElement(episode, 'actors')
            if isinstance(actors, (list, tuple)):
                for a in actors:
                    actor_el = ET.SubElement(actors_el, 'actor')
                    name = a.get('name') if isinstance(a, dict) else a
                    name_el = ET.SubElement(actor_el, 'name')
                    name_el.text = _ensure_unicode(name)
            else:
                for name in str(actors).split(','):
                    actor_el = ET.SubElement(actors_el, 'actor')
                    name_el = ET.SubElement(actor_el, 'name')
                    name_el.text = _ensure_unicode(name.strip())
        except Exception:
            pass
    if meta.get('director'):
        director_el = ET.SubElement(episode, 'director')
        director_el.text = _ensure_unicode(meta.get('director'))
    if meta.get('credits'):
        credits_el = ET.SubElement(episode, 'credits')
        credits_el.text = _ensure_unicode(meta.get('credits'))
    return episode


def generate_series_nfo(meta: Dict) -> ET.Element:
    """Generate a tvshow Element similar to Sonarr's tvshow.nfo output.
    Expected meta keys: title, rating, plot, id (tvdb), tmdb_id, imdb_id, tvmaze_id,
    genres (list), tags (list), status, premiered (YYYY-MM-DD), studio, poster_url
    """
    tvshow = ET.Element('tvshow')
    title_el = ET.SubElement(tvshow, 'title')
    title_el.text = _ensure_unicode(meta.get('title') or '')
    if meta.get('rating'):
        rating_el = ET.SubElement(tvshow, 'rating')
        rating_el.text = _ensure_unicode(meta.get('rating'))
    if meta.get('plot') or meta.get('overview'):
        plot_el = ET.SubElement(tvshow, 'plot')
        plot_el.text = _ensure_unicode(meta.get('plot') or meta.get('overview') or '')
    # mpaa placeholder
    mpaa_el = ET.SubElement(tvshow, 'mpaa')
    mpaa_el.text = _ensure_unicode(meta.get('mpaa') or '')
    # id (commonly tvdb id)
    if meta.get('id'):
        id_el = ET.SubElement(tvshow, 'id')
        id_el.text = _ensure_unicode(meta.get('id'))
    # uniqueid entries (tvdb default)
    if meta.get('id'):
        uid = ET.SubElement(tvshow, 'uniqueid')
        uid.set('type', 'tvdb')
        uid.set('default', 'true')
        uid.text = _ensure_unicode(meta.get('id'))
    if meta.get('imdb_id'):
        uid = ET.SubElement(tvshow, 'uniqueid')
        uid.set('type', 'imdb')
        uid.text = _ensure_unicode(meta.get('imdb_id'))
    if meta.get('tmdb_id'):
        uid = ET.SubElement(tvshow, 'uniqueid')
        uid.set('type', 'tmdb')
        uid.text = _ensure_unicode(meta.get('tmdb_id'))
    if meta.get('tvmaze_id'):
        uid = ET.SubElement(tvshow, 'uniqueid')
        uid.set('type', 'tvmaze')
        uid.text = _ensure_unicode(meta.get('tvmaze_id'))
    # genres
    genres = meta.get('genres')
    if genres:
        if isinstance(genres, (list, tuple)):
            for g in genres:
                ge = ET.SubElement(tvshow, 'genre')
                ge.text = _ensure_unicode(g)
        else:
            for g in str(genres).split(','):
                ge = ET.SubElement(tvshow, 'genre')
                ge.text = _ensure_unicode(g.strip())
    # tags
    tags = meta.get('tags') or meta.get('tag')
    if tags:
        if isinstance(tags, (list, tuple)):
            for t in tags:
                te = ET.SubElement(tvshow, 'tag')
                te.text = _ensure_unicode(t)
        else:
            te = ET.SubElement(tvshow, 'tag')
            te.text = _ensure_unicode(tags)
    if meta.get('status'):
        status_el = ET.SubElement(tvshow, 'status')
        status_el.text = _ensure_unicode(meta.get('status'))
    if meta.get('premiered'):
        prem = ET.SubElement(tvshow, 'premiered')
        prem.text = _ensure_unicode(meta.get('premiered'))
    if meta.get('studio'):
        studio_el = ET.SubElement(tvshow, 'studio')
        studio_el.text = _ensure_unicode(meta.get('studio'))
    # episodeguide - include JSON mapping similar to Sonarr
    try:
        if meta.get('id') or meta.get('tmdb_id') or meta.get('imdb_id') or meta.get('tvmaze_id'):
            mapping = {}
            if meta.get('id'):
                mapping['tvdb'] = _ensure_unicode(meta.get('id'))
            if meta.get('tvmaze_id'):
                mapping['tvmaze'] = _ensure_unicode(meta.get('tvmaze_id'))
            if meta.get('tmdb_id'):
                mapping['tmdb'] = _ensure_unicode(meta.get('tmdb_id'))
            if meta.get('imdb_id'):
                mapping['imdb'] = _ensure_unicode(meta.get('imdb_id'))
            if mapping:
                eg = ET.SubElement(tvshow, 'episodeguide')
                eg.text = _ensure_unicode(str(mapping).replace("'", '"'))
    except Exception:
        pass
    if meta.get('poster_url'):
        th = ET.SubElement(tvshow, 'thumb')
        th.text = _ensure_unicode(meta.get('poster_url'))
    # add studio if present
    if meta.get('studio'):
        studio_el = ET.SubElement(tvshow, 'studio')
        studio_el.text = _ensure_unicode(meta.get('studio'))
    # tags
    tags = meta.get('tags') or meta.get('tag')
    if tags:
        if isinstance(tags, (list, tuple)):
            for t in tags:
                te = ET.SubElement(tvshow, 'tag')
                te.text = _ensure_unicode(t)
        else:
            te = ET.SubElement(tvshow, 'tag')
            te.text = _ensure_unicode(tags)
    return tvshow


def _pretty_xml(element: ET.Element) -> str:
    """Return a pretty-printed XML string for the Element."""
    raw = ET.tostring(element, encoding='utf-8')
    parsed = minidom.parseString(raw)
    return parsed.toprettyxml(indent='  ', encoding='utf-8').decode('utf-8')


def write_nfo_to_path(nfo_path: str, element: ET.Element, overwrite: bool = False, mtime_source: Optional[str] = None) -> str:
    """Atomically write the given element to the exact nfo_path.
    Returns the path to the written NFO.
    """
    dirpath = os.path.dirname(nfo_path)
    base = os.path.basename(nfo_path)
    if os.path.exists(nfo_path) and not overwrite:
        return nfo_path
    # Write pretty-printed XML to temporary file then atomically move
    fd, tmp_path = tempfile.mkstemp(dir=dirpath, prefix=base + '.', suffix='.tmp')
    try:
        with os.fdopen(fd, 'wb') as fh:
            xml_str = _pretty_xml(element)
            fh.write(xml_str.encode('utf-8'))
            # If this is a tvshow element and contains an <id>, append TheTVDB URL (Sonarr-style)
            try:
                if element.tag == 'tvshow':
                    id_el = element.find('id')
                    if id_el is not None and id_el.text:
                        tvdb_url = f"https://www.thetvdb.com/?tab=series&id={id_el.text}\n"
                        fh.write(tvdb_url.encode('utf-8'))
            except Exception:
                pass
        os.replace(tmp_path, nfo_path)
        # permissions and mtime handling similar to write_nfo_for_file
        try:
            desired_mode = 0o644
            try:
                src = mtime_source
                if src and os.path.exists(src):
                    st = os.stat(src)
                    if os.geteuid() == 0:
                        try:
                            os.chown(nfo_path, st.st_uid, st.st_gid)
                        except Exception:
                            pass
                    if bool(st.st_mode & 0o020):
                        desired_mode = 0o664
            except Exception:
                pass
            try:
                os.chmod(nfo_path, desired_mode)
            except Exception:
                pass
            try:
                if mtime_source and os.path.exists(mtime_source):
                    mtime = os.path.getmtime(mtime_source)
                    os.utime(nfo_path, (mtime, mtime))
            except Exception:
                pass
        except Exception:
            pass
        return nfo_path
    finally:
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except Exception:
            pass


def write_nfo_for_file(file_path: str, element: ET.Element, overwrite: bool = False, mtime_source: Optional[str] = None) -> str:
    """Atomically write the provided ElementTree element as an .nfo adjacent to file_path.
    Returns the path to the written NFO.
    """
    dirpath = os.path.dirname(file_path)
    base, _ = os.path.splitext(os.path.basename(file_path))
    nfo_path = os.path.join(dirpath, f"{base}.nfo")
    if os.path.exists(nfo_path) and not overwrite:
        # Do not overwrite; return existing
        return nfo_path

    # Serialize element
    # atomic write via temp file in same directory
    fd, tmp_path = tempfile.mkstemp(dir=dirpath, prefix=base + '.', suffix='.nfo.tmp')
    try:
        with os.fdopen(fd, 'wb') as fh:
            xml_str = _pretty_xml(element)
            fh.write(xml_str.encode('utf-8'))
        os.replace(tmp_path, nfo_path)
        # Ensure file permissions are reasonable and, when possible, match video owner
        try:
            # Default to 0644; prefer group-writable if the source video is group-writable
            desired_mode = 0o644
            # If a source video exists, try to set ownership to match it
            try:
                src = mtime_source or file_path
                if src and os.path.exists(src):
                    st = os.stat(src)
                    # If running as root, attempt to chown the NFO to match the video
                    try:
                        if os.geteuid() == 0:
                            os.chown(nfo_path, st.st_uid, st.st_gid)
                    except Exception:
                        pass
                    # If source has group write, enable group write on NFO
                    if bool(st.st_mode & 0o020):
                        desired_mode = 0o664
            except Exception:
                pass
            try:
                os.chmod(nfo_path, desired_mode)
            except Exception:
                pass
            # set mtime
            try:
                if mtime_source and os.path.exists(mtime_source):
                    mtime = os.path.getmtime(mtime_source)
                    os.utime(nfo_path, (mtime, mtime))
                else:
                    # match video file mtime
                    if os.path.exists(file_path):
                        mtime = os.path.getmtime(file_path)
                        os.utime(nfo_path, (mtime, mtime))
            except Exception:
                pass
        except Exception:
            # best effort; don't fail on permission ops
            pass
        return nfo_path
    finally:
        # ensure tmp removed if something failed
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except Exception:
            pass
