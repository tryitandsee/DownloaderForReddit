from DownloaderForReddit.database.database_handler import DatabaseHandler
from DownloaderForReddit.database.model_manager import ModelManger
from DownloaderForReddit.database.models import Content, Post, User


def test_delete_orphaned_undownloaded_content_keeps_downloadable_and_downloaded_rows():
    db = DatabaseHandler(in_memory=True)
    with db.get_scoped_update_session() as session:
        user = User(name="example_user")
        post = Post(title="post")
        session.add_all([user, post])
        session.flush()
        missing_id = post.id + user.id + 100
        session.add_all(
            [
                Content(title="no user", post_id=post.id),
                Content(title="tracked user", post_id=post.id, user_id=user.id),
                Content(title="post gone", post_id=missing_id),
                Content(title="user gone", post_id=post.id, user_id=missing_id),
                Content(title="no owner"),
                Content(
                    title="downloaded, post gone", post_id=missing_id, downloaded=True
                ),
            ]
        )

    with db.get_scoped_session() as session:
        deleted = ModelManger.delete_orphaned_undownloaded_content(session=session)
        remaining = sorted(title for (title,) in session.query(Content.title))

    assert deleted == 3
    assert remaining == ["downloaded, post gone", "no user", "tracked user"]


def test_delete_reddit_object_deletes_every_post_and_content_past_one_batch():
    db = DatabaseHandler(in_memory=True)
    with db.get_scoped_update_session() as session:
        user = User(name="example_user")
        session.add(user)
        session.flush()
        posts = [
            Post(
                title=f"post {i}",
                author_id=user.id,
                significant_reddit_object_id=user.id,
            )
            for i in range(2000)
        ]
        session.add_all(posts)
        session.flush()
        session.add_all(
            Content(title=post.title, post_id=post.id, user_id=user.id)
            for post in posts
        )

    with db.get_scoped_session() as session:
        ModelManger.delete_reddit_object(session.query(User).one(), session=session)
        assert session.query(Post).count() == 0
        assert session.query(Content).count() == 0


def test_delete_ownerless_posts_keeps_posts_with_an_author_or_significant_object():
    db = DatabaseHandler(in_memory=True)
    with db.get_scoped_update_session() as session:
        user = User(name="example_user")
        session.add(user)
        session.flush()
        ownerless = Post(title="ownerless")
        author_gone = Post(title="author gone", author_id=user.id + 100)
        authored = Post(title="authored", author_id=user.id)
        tracked = Post(title="tracked", significant_reddit_object_id=user.id)
        created = [ownerless, author_gone, authored, tracked]
        session.add_all(created)
        session.flush()
        session.add_all(
            Content(title=post.title, post_id=post.id, downloaded=True)
            for post in created
        )

    with db.get_scoped_session() as session:
        deleted = ModelManger.delete_ownerless_posts(session=session)
        posts = sorted(title for (title,) in session.query(Post.title))
        content = sorted(title for (title,) in session.query(Content.title))

    assert deleted == 2
    assert posts == ["authored", "tracked"]
    assert content == ["authored", "tracked"]
