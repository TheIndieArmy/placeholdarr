from sqlalchemy.orm import Session
from services.postgres.models import Movie
class MovieRepository:
    def __init__(self, session: Session):
        self.session = session

    def get_by_tmdbid(self, tmdbid: int, is_4k: bool = False) -> Movie | None:
        return self.session.query(Movie).filter_by(tmdbid=tmdbid, is_4k=is_4k).first()
    
    def get_by_id(self, movieid: int) -> Movie | None:
        return self.session.query(Movie).filter_by(id=movieid).first()

    def add(self, **kwargs) -> Movie:
        movie = Movie(**kwargs)
        self.session.add(movie)
        self.session.commit()
        return movie

    def delete_by_tmdbid(self, tmdbid: int) -> bool:
        movie = self.get_by_tmdbid(tmdbid)
        if movie:
            self.session.delete(movie)
            self.session.commit()
            return True
        return False

    def update(self, tmdbid: int, **kwargs) -> Movie | None:
        movie = self.get_by_tmdbid(tmdbid)
        if movie:
            for k, v in kwargs.items():
                setattr(movie, k, v)
            self.session.commit()
        return movie

    def is_deleted(self, movie, is_deleted):
        movie.is_deleted = is_deleted
        self.session.commit()
        return movie
