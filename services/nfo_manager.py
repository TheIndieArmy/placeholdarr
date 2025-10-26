"""
NFO file generation and management for Jellyfin metadata.
Creates and updates .nfo files alongside placeholder files.
"""
import os
import xml.etree.ElementTree as ET
from typing import Union, List
from xml.dom import minidom
from sqlalchemy.orm import Session
from core.logger import logger
from services.postgres.models import Movie, Series, Season, Episode


def create_movie_nfo(movie: Movie, request_status: str = None) -> str:
    """
    Create NFO content for a movie with optional request status prefix.
    
    Args:
        movie: Movie database record
        request_status: Optional status to prefix (e.g., "Request", "Downloaded")
        
    Returns:
        str: Formatted XML NFO content
    """
    root = ET.Element("movie")
    
    # Title with optional prefix
    title = movie.title
    if request_status:
        title = f"[{request_status}] {title}"
    
    ET.SubElement(root, "title").text = title
    ET.SubElement(root, "originaltitle").text = movie.originaltitle or movie.title
    ET.SubElement(root, "sorttitle").text = movie.sorttitle or movie.title.lower()
    
    # Plot/overview with optional prefix
    plot = movie.plot or movie.jellyfin_overview or "No overview available"
    if request_status:
        plot = f"[{request_status}] {plot}"
    ET.SubElement(root, "plot").text = plot
    
    # Add other metadata
    if movie.tagline:
        ET.SubElement(root, "tagline").text = movie.tagline
    if movie.runtime:
        ET.SubElement(root, "runtime").text = str(movie.runtime)
    if movie.rating:
        ET.SubElement(root, "mpaa").text = movie.rating
    if movie.year:
        ET.SubElement(root, "year").text = str(movie.year)
    
    # IDs
    if movie.tmdbid:
        uniqueid = ET.SubElement(root, "uniqueid", type="tmdb", default="true")
        uniqueid.text = str(movie.tmdbid)
    if movie.imdb_id:
        uniqueid = ET.SubElement(root, "uniqueid", type="imdb")
        uniqueid.text = movie.imdb_id
    
    # Genres
    if movie.genres:
        for genre in movie.genres.split(','):
            ET.SubElement(root, "genre").text = genre.strip()
    
    # People
    if movie.director:
        for director in movie.director.split(','):
            ET.SubElement(root, "director").text = director.strip()
    if movie.studio:
        ET.SubElement(root, "studio").text = movie.studio
    
    # Images
    if movie.poster_url:
        thumb = ET.SubElement(root, "thumb", aspect="poster")
        thumb.text = movie.poster_url
    if movie.fanart_url:
        fanart = ET.SubElement(root, "fanart")
        thumb = ET.SubElement(fanart, "thumb")
        thumb.text = movie.fanart_url
    
    # Add dummy tag for identification
    ET.SubElement(root, "tag").text = "dummy"
    
    return _prettify_xml(root)


def create_series_nfo(series: Series, request_status: str = None) -> str:
    """
    Create NFO content for a TV series.
    
    Args:
        series: Series database record
        request_status: Optional status to prefix
        
    Returns:
        str: Formatted XML NFO content
    """
    root = ET.Element("tvshow")
    
    # Title with optional prefix
    title = series.title
    if request_status:
        title = f"[{request_status}] {title}"
    
    ET.SubElement(root, "title").text = title
    ET.SubElement(root, "originaltitle").text = series.originaltitle or series.title
    ET.SubElement(root, "sorttitle").text = series.sorttitle or series.title.lower()
    
    # Plot/overview with optional prefix
    plot = series.plot or series.jellyfin_overview or "No overview available"
    if request_status:
        plot = f"[{request_status}] {plot}"
    ET.SubElement(root, "plot").text = plot
    
    # Add other metadata
    if series.rating:
        ET.SubElement(root, "mpaa").text = series.rating
    if series.year:
        ET.SubElement(root, "year").text = str(series.year)
    if series.premiered:
        # Handle both string and datetime objects
        if hasattr(series.premiered, 'isoformat'):
            ET.SubElement(root, "premiered").text = series.premiered.isoformat()
        else:
            ET.SubElement(root, "premiered").text = str(series.premiered)
    if series.ended:
        # Handle both string and datetime objects
        if hasattr(series.ended, 'isoformat'):
            ET.SubElement(root, "enddate").text = series.ended.isoformat()
        else:
            ET.SubElement(root, "enddate").text = str(series.ended)
    
    # IDs  
    if series.tvdbid:
        uniqueid = ET.SubElement(root, "uniqueid", type="tvdb", default="true")
        uniqueid.text = str(series.tvdbid)
    if series.imdb_id:
        uniqueid = ET.SubElement(root, "uniqueid", type="imdb")
        uniqueid.text = series.imdb_id
    
    # Genres
    if series.genres:
        for genre in series.genres.split(','):
            ET.SubElement(root, "genre").text = genre.strip()
    
    # Studio/Network
    if series.studio:
        ET.SubElement(root, "studio").text = series.studio
    
    # Images
    if series.poster_url:
        thumb = ET.SubElement(root, "thumb", aspect="poster")
        thumb.text = series.poster_url
    if series.fanart_url:
        fanart = ET.SubElement(root, "fanart")
        thumb = ET.SubElement(fanart, "thumb")
        thumb.text = series.fanart_url
    
    # Add dummy tag
    ET.SubElement(root, "tag").text = "dummy"
    
    return _prettify_xml(root)


def create_season_nfo(season: Season, request_status: str = None) -> str:
    """
    Create NFO content for a TV season.
    
    Args:
        season: Season database record
        request_status: Optional status to prefix
        
    Returns:
        str: Formatted XML NFO content
    """
    root = ET.Element("season")
    
    # Title with optional prefix
    title = season.title
    if request_status:
        title = f"[{request_status}] {title}"
    
    ET.SubElement(root, "title").text = title
    ET.SubElement(root, "seasonnumber").text = str(season.season_number)
    
    # Plot/overview with optional prefix
    plot = season.plot or season.jellyfin_overview or "No overview available"
    if request_status:
        plot = f"[{request_status}] {plot}"
    ET.SubElement(root, "plot").text = plot
    
    # Add year
    if season.year:
        ET.SubElement(root, "year").text = str(season.year)
    
    # IDs
    if season.tvdbid:
        uniqueid = ET.SubElement(root, "uniqueid", type="tvdb", default="true")
        uniqueid.text = str(season.tvdbid)
    if season.imdb_id:
        uniqueid = ET.SubElement(root, "uniqueid", type="imdb")
        uniqueid.text = season.imdb_id
    
    # Images
    if season.poster_url:
        thumb = ET.SubElement(root, "thumb", aspect="poster")
        thumb.text = season.poster_url
    if season.fanart_url:
        fanart = ET.SubElement(root, "fanart")
        thumb = ET.SubElement(fanart, "thumb")
        thumb.text = season.fanart_url
    
    # Add dummy tag
    ET.SubElement(root, "tag").text = "dummy"
    
    return _prettify_xml(root)


def create_episode_nfo(episode: Episode, request_status: str = None) -> str:
    """
    Create NFO content for a TV episode.
    
    Args:
        episode: Episode database record
        request_status: Optional status to prefix
        
    Returns:
        str: Formatted XML NFO content
    """
    root = ET.Element("episodedetails")
    
    # Title with optional prefix
    title = episode.title
    if request_status:
        title = f"[{request_status}] {title}"
    
    ET.SubElement(root, "title").text = title
    ET.SubElement(root, "episode").text = str(episode.episode_number)
    ET.SubElement(root, "season").text = str(episode.season.season_number)
    
    # Plot/overview with optional prefix
    plot = episode.plot or episode.jellyfin_overview or "No overview available"
    if request_status:
        plot = f"[{request_status}] {plot}"
    ET.SubElement(root, "plot").text = plot
    
    # Add other metadata
    if episode.runtime:
        ET.SubElement(root, "runtime").text = str(episode.runtime)
    if episode.rating:
        ET.SubElement(root, "mpaa").text = episode.rating
    if episode.air_date:
        # Handle both string and datetime objects
        if hasattr(episode.air_date, 'isoformat'):
            ET.SubElement(root, "aired").text = episode.air_date.isoformat()
        else:
            ET.SubElement(root, "aired").text = str(episode.air_date)
    
    # IDs
    if episode.tvdbid:
        uniqueid = ET.SubElement(root, "uniqueid", type="tvdb", default="true")
        uniqueid.text = str(episode.tvdbid)
    if episode.imdb_id:
        uniqueid = ET.SubElement(root, "uniqueid", type="imdb")
        uniqueid.text = episode.imdb_id
    
    # People
    if episode.directors:
        for director in episode.directors.split(','):
            ET.SubElement(root, "director").text = director.strip()
    if episode.writers:
        for writer in episode.writers.split(','):
            ET.SubElement(root, "credits").text = writer.strip()
    
    # Images
    if episode.thumb_url:
        thumb = ET.SubElement(root, "thumb")
        thumb.text = episode.thumb_url
    
    # Add dummy tag
    ET.SubElement(root, "tag").text = "dummy"
    
    return _prettify_xml(root)


def write_nfo_file(nfo_content: str, nfo_path: str) -> bool:
    """
    Write NFO content to file.
    
    Args:
        nfo_content: XML content to write
        nfo_path: Path where to write the NFO file
        
    Returns:
        bool: True if successful, False otherwise
    """
    try:
        # Ensure directory exists
        os.makedirs(os.path.dirname(nfo_path), exist_ok=True)
        
        with open(nfo_path, 'w', encoding='utf-8') as f:
            f.write(nfo_content)
        
        logger.info(f"Created NFO file: {nfo_path}", extra={'emoji_type': 'success'})
        return True
        
    except Exception as e:
        logger.error(f"Failed to create NFO file {nfo_path}: {e}", extra={'emoji_type': 'error'})
        return False


def update_nfo_status(nfo_path: str, new_status: str) -> bool:
    """
    Update the status prefix in an existing NFO file.
    
    Args:
        nfo_path: Path to the NFO file
        new_status: New status to set (e.g., "Downloaded", "Request")
        
    Returns:
        bool: True if successful, False otherwise
    """
    try:
        if not os.path.exists(nfo_path):
            logger.warning(f"NFO file not found: {nfo_path}", extra={'emoji_type': 'warning'})
            return False
        
        # Parse existing NFO
        tree = ET.parse(nfo_path)
        root = tree.getroot()
        
        # Update title
        title_elem = root.find('title')
        if title_elem is not None and title_elem.text:
            # Remove existing status prefix if any
            title = title_elem.text
            if title.startswith('[') and ']' in title:
                title = title.split(']', 1)[1].strip()
            
            # Add new status prefix
            title_elem.text = f"[{new_status}] {title}"
        
        # Update plot/overview
        plot_elem = root.find('plot')
        if plot_elem is not None and plot_elem.text:
            # Remove existing status prefix if any
            plot = plot_elem.text
            if plot.startswith('[') and ']' in plot:
                plot = plot.split(']', 1)[1].strip()
            
            # Add new status prefix
            plot_elem.text = f"[{new_status}] {plot}"
        
        # Write back to file
        xml_str = _prettify_xml(root)
        with open(nfo_path, 'w', encoding='utf-8') as f:
            f.write(xml_str)
        
        logger.info(f"Updated NFO status to '{new_status}': {nfo_path}", extra={'emoji_type': 'success'})
        return True
        
    except Exception as e:
        logger.error(f"Failed to update NFO file {nfo_path}: {e}", extra={'emoji_type': 'error'})
        return False


def delete_nfo_file(nfo_path: str) -> bool:
    """
    Delete an NFO file with verification.
    
    Args:
        nfo_path: Path to the NFO file to delete
        
    Returns:
        bool: True if successful and verified, False otherwise
    """
    try:
        if os.path.exists(nfo_path):
            logger.info(f"🗑️ Deleting NFO file: {nfo_path}", extra={'emoji_type': 'delete'})
            os.remove(nfo_path)
            
            # Verification check - make sure file is actually gone
            if os.path.exists(nfo_path):
                logger.error(f"❌ VERIFICATION FAILED: NFO file still exists after deletion: {nfo_path}", extra={'emoji_type': 'error'})
                return False
                
            logger.info(f"✅ Successfully deleted and verified NFO file: {nfo_path}", extra={'emoji_type': 'success'})
        else:
            logger.debug(f"NFO file doesn't exist, nothing to delete: {nfo_path}", extra={'emoji_type': 'debug'})
            
        return True
    except Exception as e:
        logger.error(f"❌ Failed to delete NFO file {nfo_path}: {e}", extra={'emoji_type': 'error'})
        return False


def get_nfo_path(placeholder_path: str) -> str:
    """
    Get the NFO file path for a given placeholder file path.
    
    Args:
        placeholder_path: Path to the placeholder file
        
    Returns:
        str: Path where the NFO file should be located
    """
    if placeholder_path:
        base_path = os.path.splitext(placeholder_path)[0]
        return f"{base_path}.nfo"
    return None


def _prettify_xml(elem: ET.Element) -> str:
    """
    Convert XML element to pretty-printed string.
    
    Args:
        elem: XML element to prettify
        
    Returns:
        str: Pretty-printed XML string
    """
    rough_string = ET.tostring(elem, 'utf-8')
    reparsed = minidom.parseString(rough_string)
    return reparsed.toprettyxml(indent="  ", encoding=None)
