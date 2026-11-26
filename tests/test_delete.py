#
# Gramps Web API - A RESTful API for the Gramps genealogy program
#
# Copyright (C) 2026      David Straub
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation; either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program. If not, see <https://www.gnu.org/licenses/>.
#

"""Tests for deleting objects with references."""

from unittest.mock import MagicMock

import pytest
from gramps.gen.db import DbTxn
from gramps.gen.db.utils import make_database
from gramps.gen.errors import HandleError
from gramps.gen.lib import (
    Citation,
    Event,
    Family,
    Media,
    Note,
    Person,
    Place,
    Repository,
    Source,
    StyledText,
    StyledTextTag,
    StyledTextTagType,
)

from gramps_webapi.api.resources.delete import (
    delete_family,
    delete_object_in_transaction,
)


class FakeDb:
    """A database whose reference table holds an entry for a person that is gone."""

    def __init__(self, backlinks):
        self.backlinks = backlinks
        self.commit_note = MagicMock()
        self.remove_family = MagicMock()

    def find_backlink_handles(self, handle, include_classes=None):
        return iter(self.backlinks)

    def get_person_from_handle(self, handle):
        raise HandleError(f"Handle {handle} not found")

    def get_note_from_handle(self, handle):
        return MagicMock()

    def method(self, fmt, *args):
        return getattr(self, fmt % tuple(arg.lower() for arg in args))


def test_delete_family_skips_a_backlink_whose_object_is_gone():
    db_handle = FakeDb([("Person", "gone"), ("Note", "n0001")])
    trans = MagicMock()

    delete_family(db_handle, "f0001", trans)

    # the intact backlink is still cleaned up, and the family is gone
    db_handle.commit_note.assert_called_once()
    db_handle.remove_family.assert_called_once_with("f0001", trans)


# A link in the text of a note is a reference to its target. Deleting the
# target has to remove the link, and only the link.


@pytest.fixture(scope="module")
def db():
    """An empty in-memory tree."""
    database = make_database("sqlite")
    database.load(":memory:")
    yield database
    database.close()


def _add(db, obj) -> str:
    with DbTxn("add", db) as trans:
        db.method("add_%s", type(obj).__name__)(obj, trans)
    return obj.handle


def _link(class_name: str, handle: str, start: int, end: int) -> StyledTextTag:
    return StyledTextTag(
        StyledTextTagType.LINK, f"gramps://{class_name}/handle/{handle}", [(start, end)]
    )


def _linked_note(db, class_name: str, handle: str) -> str:
    """Add a note that links to an object, with some other styling next to it."""
    note = Note()
    note.set_styledtext(
        StyledText(
            "See this record.",
            [
                StyledTextTag(StyledTextTagType.BOLD, True, [(0, 3)]),
                _link(class_name, handle, 4, 15),
            ],
        )
    )
    return _add(db, note)


def _delete(db, class_name: str, handle: str) -> None:
    with DbTxn("delete", db) as trans:
        delete_object_in_transaction(db, handle, class_name, trans)


def _assert_link_removed(db, note_handle: str, target_handle: str) -> None:
    note = db.get_note_from_handle(note_handle)
    assert str(note.text) == "See this record."
    assert [int(tag.name) for tag in note.text.get_tags()] == [StyledTextTagType.BOLD]
    assert list(db.find_backlink_handles(target_handle)) == []


@pytest.mark.parametrize(
    "cls", [Person, Family, Event, Place, Source, Repository, Media, Note]
)
def test_delete_removes_note_links(db, cls):
    target = _add(db, cls())
    note = _linked_note(db, cls.__name__, target)
    assert ("Note", note) in list(db.find_backlink_handles(target))

    _delete(db, cls.__name__, target)

    _assert_link_removed(db, note, target)


def test_delete_citation_removes_note_links(db):
    citation = Citation()
    citation.set_reference_handle(_add(db, Source()))
    target = _add(db, citation)
    note = _linked_note(db, "Citation", target)

    _delete(db, "Citation", target)

    _assert_link_removed(db, note, target)


def test_delete_source_removes_note_links_to_its_citations(db):
    source = _add(db, Source())
    citation = Citation()
    citation.set_reference_handle(source)
    citation_handle = _add(db, citation)
    note_on_source = _linked_note(db, "Source", source)
    note_on_citation = _linked_note(db, "Citation", citation_handle)

    _delete(db, "Source", source)

    # the citation goes with its source
    assert not db.has_citation_handle(citation_handle)
    _assert_link_removed(db, note_on_source, source)
    _assert_link_removed(db, note_on_citation, citation_handle)


def test_delete_keeps_note_links_to_other_objects(db):
    kept = _add(db, Event())
    gone = _add(db, Event())
    note = Note()
    note.set_styledtext(
        StyledText(
            "One and two.", [_link("Event", kept, 0, 3), _link("Event", gone, 8, 11)]
        )
    )
    note_handle = _add(db, note)

    _delete(db, "Event", gone)

    tags = db.get_note_from_handle(note_handle).text.get_tags()
    assert [tag.value for tag in tags] == [f"gramps://Event/handle/{kept}"]
    assert list(db.find_backlink_handles(kept)) == [("Note", note_handle)]


def test_delete_note_that_links_to_itself(db):
    note = Note()
    handle = _add(db, note)
    note.set_styledtext(StyledText("Itself.", [_link("Note", handle, 0, 6)]))
    with DbTxn("edit", db) as trans:
        db.commit_note(note, trans)

    _delete(db, "Note", handle)

    assert not db.has_note_handle(handle)


@pytest.mark.parametrize(
    "ranges",
    [[[(0, 5)], [(10, 16)]], [[(0, 5), (10, 16)]]],
    ids=["two tags", "one tag with two ranges"],
)
def test_delete_removes_every_link_of_a_note_to_the_object(db, ranges):
    target = _add(db, Event())
    value = f"gramps://Event/handle/{target}"
    note = Note()
    note.set_styledtext(
        StyledText(
            "First and second.",
            [StyledTextTag(StyledTextTagType.LINK, value, spans) for spans in ranges],
        )
    )
    note_handle = _add(db, note)
    # the note is one referent of the object, however often it links to it
    assert list(db.find_backlink_handles(target)) == [("Note", note_handle)]

    _delete(db, "Event", target)

    note = db.get_note_from_handle(note_handle)
    assert str(note.text) == "First and second."
    assert note.text.get_tags() == []
    assert list(db.find_backlink_handles(target)) == []


def test_delete_leaves_links_by_gramps_id(db):
    # A link by Gramps ID is no reference in Gramps, so the note is not among
    # the object's backlinks for it. Delete leaves that link, as Gramps
    # desktop does, and check and repair removes it.
    event = Event()
    target = _add(db, event)
    by_id = f"gramps://Event/gramps_id/{event.gramps_id}"
    note = Note()
    note.set_styledtext(
        StyledText(
            "First and second.",
            [
                _link("Event", target, 0, 5),
                StyledTextTag(StyledTextTagType.LINK, by_id, [(10, 16)]),
            ],
        )
    )
    note_handle = _add(db, note)

    _delete(db, "Event", target)

    tags = db.get_note_from_handle(note_handle).text.get_tags()
    assert [tag.value for tag in tags] == [by_id]
