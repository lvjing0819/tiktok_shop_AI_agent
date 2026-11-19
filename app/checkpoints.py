"""Open a persistent graph saver, using PostgreSQL in deployment."""
import os
from contextlib import contextmanager
from app.store import database_url


@contextmanager
def saver():
    url = database_url()
    if url.startswith('postgresql'):
        from langgraph.checkpoint.postgres import PostgresSaver
        with PostgresSaver.from_conn_string(url.replace('postgresql+psycopg://', 'postgresql://', 1)) as cp:
            cp.setup()
            yield cp
    else:
        from langgraph.checkpoint.sqlite import SqliteSaver
        with SqliteSaver.from_conn_string(os.getenv('CHECKPOINT_PATH', os.getenv('DATABASE_PATH', 'shop.sqlite') + '.checkpoints.sqlite')) as cp:
            cp.setup()
            yield cp
