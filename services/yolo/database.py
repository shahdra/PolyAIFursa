import logging
import os

from sqlalchemy import Column, create_engine, ForeignKey, Float, String, DateTime, Integer, func
from sqlalchemy.orm import declarative_base, relationship, sessionmaker

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

Base = declarative_base()


class PredictionSession(Base):
    __tablename__ = "prediction_sessions"

    uid = Column(String, primary_key=True, index=True)
    timestamp = Column(DateTime, server_default=func.now(), nullable=False)
    original_image = Column(String, nullable=False)
    predicted_image = Column(String, nullable=False)
    detection_objects = relationship(
        "DetectionObject",
        back_populates="prediction_session",
        cascade="all, delete-orphan",
    )


class DetectionObject(Base):
    __tablename__ = "detection_objects"

    id = Column(Integer, primary_key=True, autoincrement=True)
    prediction_uid = Column(
        String,
        ForeignKey("prediction_sessions.uid", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    label = Column(String, nullable=False, index=True)
    score = Column(Float, nullable=False, index=True)
    box = Column(String, nullable=False)
    prediction_session = relationship("PredictionSession", back_populates="detection_objects")


engine = None
SessionLocal = None


def get_database_url(db_path: str = "predictions.db"):
    backend = os.environ.get("DB_BACKEND", "sqlite").lower()

    if backend in {"postgres", "postgresql"}:
        db_user = os.environ.get("DB_USER", "user")
        db_password = os.environ.get("DB_PASSWORD", "pass")
        db_host = os.environ.get("DB_HOST", "localhost")
        db_port = os.environ.get("DB_PORT", "5432")
        db_name = os.environ.get("DB_NAME", "predictions")
        return (
            f"postgresql+psycopg://{db_user}:{db_password}@"
            f"{db_host}:{db_port}/{db_name}"
        )

    return f"sqlite:///{db_path}"


def init_db(db_path: str = "predictions.db"):
    global engine, SessionLocal
    database_url = get_database_url(db_path)
    backend = os.environ.get("DB_BACKEND", "sqlite").lower()
    db_type = "postgres" if backend in {"postgres", "postgresql"} else "sqlite"

    logger.info("Using database backend: %s", db_type)
    
    engine = create_engine(
        database_url,
        connect_args={"check_same_thread": False} if database_url.startswith("sqlite") else {},
        pool_pre_ping=True,
    )
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    Base.metadata.create_all(bind=engine)


def get_db():
    if SessionLocal is None:
        init_db()
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def save_prediction_session(uid, original_image, predicted_image):
    if SessionLocal is None:
        init_db()
    db = SessionLocal()
    try:
        db.add(
            PredictionSession(
                uid=uid,
                original_image=original_image,
                predicted_image=predicted_image,
            )
        )
        db.commit()
    finally:
        db.close()


def save_detection_object(prediction_uid, label, score, box):
    if SessionLocal is None:
        init_db()
    db = SessionLocal()
    try:
        db.add(
            DetectionObject(
                prediction_uid=prediction_uid,
                label=label,
                score=score,
                box=str(box),
            )
        )
        db.commit()
    finally:
        db.close()
