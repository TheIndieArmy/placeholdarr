from sqlalchemy.orm import Session
from core.config import settings
from services.postgres.models import Movie
class MovieRepository:
    def __init__(self, session: Session):
        self.session = session

    def get_by_tmdbid(self, tmdbid: int, instance_key: str | None = None, instance_id: str | None = None) -> Movie | None:
        if instance_id:
            return self.session.query(Movie).filter_by(tmdbid=tmdbid, instance_id=str(instance_id).strip().lower()).first()
        if instance_key:
            return self.session.query(Movie).filter_by(tmdbid=tmdbid, instance_key=str(instance_key).strip().lower()).first()
        return self.session.query(Movie).filter_by(tmdbid=tmdbid).first()
    
    def get_by_id(self, movieid: int) -> Movie | None:
        return self.session.query(Movie).filter_by(id=movieid).first()

    def add(self, **kwargs) -> Movie:
        if 'instance_id' not in kwargs:
            if 'instance_key' in kwargs and kwargs.get('instance_key'):
                key = str(kwargs.get('instance_key') or '').strip().lower()
                item = settings.resolve_arr_instance('radarr', instance_key=key) or {}
                kwargs['instance_id'] = str(item.get('instance_id') or f"radarr:{key}").strip().lower()
            else:
                item = settings.resolve_arr_instance('radarr', role='primary') or {}
                kwargs['instance_id'] = str(item.get('instance_id') or 'radarr:primary').strip().lower()
        if 'instance_key' not in kwargs:
            item = settings.resolve_arr_instance('radarr', instance_id=kwargs.get('instance_id')) or settings.resolve_arr_instance('radarr', role='primary') or {}
            kwargs['instance_key'] = str(item.get('instance_key') or 'radarr_std').strip().lower()
        movie = Movie(**kwargs)
        self.session.add(movie)
        self.session.commit()
        return movie

    def delete_by_tmdbid(self, tmdbid: int, instance_id: str | None = None, instance_key: str | None = None) -> bool:
        movie = self.get_by_tmdbid(tmdbid, instance_id=instance_id, instance_key=instance_key)
        if movie:
            self.session.delete(movie)
            self.session.commit()
            return True
        return False

    def update(self, tmdbid: int, instance_id: str | None = None, instance_key: str | None = None, **kwargs) -> Movie | None:
        movie = self.get_by_tmdbid(tmdbid, instance_id=instance_id, instance_key=instance_key)
        if movie:
            for k, v in kwargs.items():
                setattr(movie, k, v)
            self.session.commit()
        return movie

    def is_deleted(self, movie, is_deleted):
        movie.is_deleted = is_deleted
        self.session.commit()
        return movie
