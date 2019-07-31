#
# This source file is part of the EdgeDB open source project.
#
# Copyright 2008-present MagicStack Inc. and the EdgeDB authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#


from edb.common import struct
from edb.edgeql import ast as qlast

from edb import errors

from . import delta as sd
from . import derivable
from . import objects as so
from . import utils


class InheritingObjectCommand(sd.ObjectCommand):
    pass


def delta_bases(old_bases, new_bases):
    dropped = frozenset(old_bases) - frozenset(new_bases)
    removed_bases = [so.ObjectRef(name=b) for b in dropped]
    common_bases = [b for b in old_bases if b not in dropped]

    added_bases = []

    j = 0

    added_set = set()
    added_base_refs = []

    if common_bases:
        for i, base in enumerate(new_bases):
            if common_bases[j] == base:
                # Found common base, insert the accumulated
                # list of new bases and continue
                if added_base_refs:
                    ref = so.ObjectRef(name=common_bases[j])
                    added_bases.append((added_base_refs, ('BEFORE', ref)))
                    added_base_refs = []
                j += 1
                if j >= len(common_bases):
                    break
                else:
                    continue

            # Base has been inserted at position j
            added_base_refs.append(so.ObjectRef(name=base))
            added_set.add(base)

    # Finally, add all remaining bases to the end of the list
    tail_bases = added_base_refs + [
        so.ObjectRef(name=b) for b in new_bases
        if b not in added_set and b not in common_bases
    ]

    if tail_bases:
        added_bases.append((tail_bases, 'LAST'))

    return tuple(removed_bases), tuple(added_bases)


class AlterInherit(sd.Command):
    astnode = qlast.AlterAddInherit, qlast.AlterDropInherit

    @classmethod
    def _cmd_tree_from_ast(cls, schema, astnode, context):
        # The base changes are handled by AlterNamedObject
        return None


class CreateInheritingObject(InheritingObjectCommand, sd.CreateObject):
    def _create_begin(self, schema, context):
        schema = super()._create_begin(schema, context)
        ancestors = self.scls.get_ancestors(schema)
        if ancestors is None:
            bases = self.scls.get_bases(schema)
            if bases:
                ancestors = compute_mro(schema, self.scls)[1:]
                schema = self.scls.set_field_value(
                    schema, 'ancestors', ancestors)

                if not self.has_attribute_value('ancestors'):
                    self.set_attribute_value('ancestors', ancestors)

        return schema

    def _create_finalize(self, schema, context):
        schema = self.scls.acquire_ancestor_inheritance(schema, dctx=context)
        schema = self.scls.update_descendants(schema)
        return super()._create_finalize(schema, context)

    @classmethod
    def _cmd_tree_from_ast(cls, schema, astnode, context):
        cmd = super()._cmd_tree_from_ast(schema, astnode, context)

        bases = cls._classbases_from_ast(schema, astnode, context)
        if bases is not None:
            cmd.add(
                sd.AlterObjectProperty(
                    property='bases',
                    new_value=bases
                )
            )

        if getattr(astnode, 'is_final', False):
            cmd.add(sd.AlterObjectProperty(
                property='is_final',
                new_value=True
            ))

        return cmd

    @classmethod
    def _classbases_from_ast(cls, schema, astnode, context):
        modaliases = context.modaliases

        base_refs = [
            utils.ast_to_typeref(b, modaliases=modaliases, schema=schema)
            for b in getattr(astnode, 'bases', None) or []
        ]

        return cls._validate_base_refs(schema, base_refs, astnode, context)

    @classmethod
    def _validate_base_refs(cls, schema, base_refs, astnode, context):
        classname = cls._classname_from_ast(schema, astnode, context)

        bases = so.ObjectList.create(schema, base_refs)

        for base in bases.objects(schema):
            if base.is_type() and base.contains_any(schema):
                base_type_name = base.get_displayname(schema)
                raise errors.SchemaError(
                    f"{base_type_name!r} cannot be a parent type")

        mcls = cls.get_schema_metaclass()
        if not bases and classname not in mcls.get_root_classes():
            default_base = mcls.get_default_base_name()

            if default_base is not None and classname != default_base:
                default_base = schema.get(default_base)
                bases = so.ObjectList.create(
                    schema,
                    [utils.reduce_to_typeref(schema, default_base)])

        return bases


class AlterInheritingObject(InheritingObjectCommand, sd.AlterObject):
    def _alter_begin(self, schema, context, scls):
        schema = super()._alter_begin(schema, context, scls)

        for op in self.get_subcommands(type=RebaseInheritingObject):
            schema, _ = op.apply(schema, context)

        schema = scls.acquire_ancestor_inheritance(schema)

        return schema

    def _alter_finalize(self, schema, context, scls):
        schema = self.scls.acquire_ancestor_inheritance(schema, dctx=context)
        schema = self.scls.update_descendants(schema)
        return super()._alter_finalize(schema, context, scls)


class DeleteInheritingObject(InheritingObjectCommand, sd.DeleteObject):

    def _delete_finalize(self, schema, context, scls):
        schema = self.scls.update_descendants(schema)
        return super()._delete_finalize(schema, context, scls)


class RebaseInheritingObject(sd.ObjectCommand):
    _delta_action = 'rebase'

    new_base = struct.Field(tuple, default=tuple())
    removed_bases = struct.Field(tuple)
    added_bases = struct.Field(tuple)

    def __repr__(self):
        return '<%s.%s "%s">' % (self.__class__.__module__,
                                 self.__class__.__name__,
                                 self.classname)

    def apply(self, schema, context):
        metaclass = self.get_schema_metaclass()
        scls = self.get_object(schema)
        self.scls = scls

        objects = [scls] + list(scls.descendants(schema))
        for obj in objects:
            for refdict in scls.__class__.get_refdicts():
                attr = refdict.attr
                local_attr = refdict.local_attr
                backref = refdict.backref_attr

                coll = obj.get_field_value(schema, attr)
                local_coll = obj.get_field_value(schema, local_attr)

                for ref_name in tuple(coll.keys(schema)):
                    if not local_coll.has(schema, ref_name):
                        try:
                            obj.get_classref_origin(
                                schema, ref_name, attr, local_attr, backref)
                        except KeyError:
                            schema, coll = coll.delete(schema, [ref_name])

        schema, props = self._get_field_updates(schema, context)
        schema = scls.update(schema, props)

        for op in self.get_subcommands(type=sd.ObjectCommand):
            schema, _ = op.apply(schema, context)

        bases = list(scls.get_bases(schema).objects(schema))
        default_base_name = scls.get_default_base_name()
        if default_base_name:
            default_base = schema.get(default_base_name)
            if bases == [default_base]:
                bases = []

        removed_bases = {b.get_name(schema) for b in self.removed_bases}
        existing_bases = set()

        for b in bases:
            if b.get_name(schema) in removed_bases:
                bases.remove(b)
            else:
                existing_bases.add(b.get_name(schema))

        index = {b.get_name(schema): i for i, b in enumerate(bases)}

        for new_bases, pos in self.added_bases:
            if isinstance(pos, tuple):
                pos, ref = pos

            if pos is None or pos == 'LAST':
                idx = len(bases)
            elif pos == 'FIRST':
                idx = 0
            else:
                idx = index[ref]

            bases[idx:idx] = [self.get_object(schema, name=b.get_name(schema))
                              for b in new_bases
                              if b.get_name(schema) not in existing_bases]
            index = {b.get_name(schema): i for i, b in enumerate(bases)}

        if not bases:
            default = metaclass.get_default_base_name()
            if default:
                bases = [self.get_object(schema, name=default)]
            else:
                bases = []

        schema = scls.set_field_value(schema, 'bases', bases)
        new_ancestors = compute_mro(schema, scls)[1:]
        schema = scls.set_field_value(schema, 'ancestors', new_ancestors)

        if not self.has_attribute_value('bases'):
            self.set_attribute_value('bases', bases)

        if not self.has_attribute_value('ancestors'):
            self.set_attribute_value('ancestors', new_ancestors)

        alter_cmd = sd.ObjectCommandMeta.get_command_class(
            sd.AlterObject, metaclass)

        descendants = list(scls.descendants(schema))
        if descendants and not list(self.get_subcommands(type=alter_cmd)):
            for descendant in descendants:
                new_ancestors = compute_mro(schema, descendant)[1:]
                schema = descendant.set_field_value(
                    schema, 'ancestors', new_ancestors)
                alter = alter_cmd(classname=descendant.get_name(schema))
                alter.add(sd.AlterObjectProperty(
                    property='ancestors',
                    new_value=new_ancestors,
                ))
                self.add(alter)

        return schema, scls


def _merge_mro(schema, obj, mros):
    result = []

    while True:
        nonempty = [mro for mro in mros if mro]
        if not nonempty:
            return result

        for mro in nonempty:
            candidate = mro[0]
            tails = [m for m in nonempty
                     if id(candidate) in {id(c) for c in m[1:]}]
            if not tails:
                break
        else:
            raise errors.SchemaError(
                f"Could not find consistent ancestor order for "
                f"{obj.get_verbosename(schema)}"
            )

        result.append(candidate)

        for mro in nonempty:
            if mro[0] is candidate:
                del mro[0]

    return result


def compute_mro(schema, obj):
    bases = tuple(obj.get_bases(schema).objects(schema))
    mros = [[obj]]

    for base in bases:
        mros.append(base.compute_mro(schema))

    return _merge_mro(schema, obj, mros)


class InheritingObject(derivable.DerivableObject):
    bases = so.SchemaField(
        so.ObjectList,
        default=so.ObjectList,
        coerce=True, inheritable=False, compcoef=0.714)

    ancestors = so.SchemaField(
        so.ObjectList,
        coerce=True, default=None, hashable=False)

    is_abstract = so.SchemaField(
        bool,
        default=False,
        inheritable=False, compcoef=0.909)

    is_derived = so.SchemaField(
        bool,
        default=False, compcoef=0.909)

    derived_from = so.SchemaField(
        so.Object,
        default=None, compcoef=0.909, inheritable=False)

    is_final = so.SchemaField(
        bool,
        default=False, compcoef=0.909)

    @classmethod
    def delta(cls, old, new, *, context=None, old_schema, new_schema):
        if context is None:
            context = so.ComparisonContext()

        with context(old, new):
            delta = super().delta(old, new, context=context,
                                  old_schema=old_schema,
                                  new_schema=new_schema)

            if old and new:
                rebase = sd.ObjectCommandMeta.get_command_class(
                    RebaseInheritingObject, type(new))

                old_base_names = old.get_base_names(old_schema)
                new_base_names = new.get_base_names(new_schema)

                if old_base_names != new_base_names and rebase is not None:
                    removed, added = delta_bases(
                        old_base_names, new_base_names)

                    delta.add(rebase(
                        classname=new.get_name(new_schema),
                        metaclass=type(new),
                        removed_bases=removed,
                        added_bases=added,
                        new_base=tuple(new_base_names)))

        return delta

    def merge(self, *objs, schema, dctx=None):
        schema = super().merge(*objs, schema=schema, dctx=None)

        for obj in objs:
            for refdict in self.__class__.get_refdicts():
                # Merge Object references in each registered collection.
                #
                this_coll = self.get_explicit_field_value(
                    schema, refdict.attr, None)

                other_coll = obj.get_explicit_field_value(
                    schema, refdict.attr, None)

                if other_coll is None:
                    continue

                if refdict.non_inheritable_attr:
                    non_inh_coll = obj.get_explicit_field_value(
                        schema, refdict.non_inheritable_attr, None)

                    if non_inh_coll:
                        other_coll = type(other_coll).create(schema, {
                            v for k, v in other_coll.items(schema)
                            if not non_inh_coll.has(schema, k)
                        })

                if not other_coll:
                    continue

                if this_coll is None:
                    schema = self.set_field_value(
                        schema, refdict.attr, other_coll)
                else:
                    updates = {v for k, v in other_coll.items(schema)
                               if not this_coll.has(schema, k)}

                    schema, this_coll = this_coll.update(schema, updates)
                    schema = self.set_field_value(
                        schema, refdict.attr, this_coll)

        return schema

    def begin_classref_dict_merge(self, schema, bases, attr):
        return schema, None

    def finish_classref_dict_merge(self, schema, bases, attr):
        return schema

    def merge_classref_dict(self, schema, *,
                            bases, attr, local_attr,
                            backref_attr, classrefcls,
                            classref_keys, requires_explicit_inherit,
                            dctx=None):
        """Merge reference collections from bases.

        :param schema:         The schema.

        :param bases:          An iterable containing base objects.

        :param str attr:       Name of the attribute containing the full
                               reference collection.

        :param str local_attr: Name of the attribute containing the collection
                               of references defined locally (not inherited).

        :param str backref_attr: Name of the attribute on a referenced
                                 object containing the reference back to
                                 this object.

        :param classrefcls:    Referenced object class.

        :param classrefkeys:   An optional list of reference keys to consider
                               for merging.  If not specified, all keys
                               in the collection will be used.
        """
        classrefs = self.get_explicit_field_value(schema, attr, None)
        colltype = type(self).get_field(local_attr).type
        if classrefs is None:
            classrefs = colltype.create_empty()

        local_classrefs = self.get_explicit_field_value(
            schema, local_attr, None)
        if local_classrefs is None:
            local_classrefs = colltype.create_empty()

        if classref_keys is None:
            classref_keys = classrefs.keys(schema)

        for classref_key in classref_keys:
            local = local_classrefs.get(schema, classref_key, None)
            local_schema = schema

            inherited = []
            for b in bases:
                attrval = b.get_explicit_field_value(schema, attr, None)
                if not attrval:
                    continue
                bref = attrval.get(schema, classref_key, None)
                if bref is not None:
                    inherited.append(bref)

            ancestry = {pref.get_field_value(schema, backref_attr): pref
                        for pref in inherited}

            inherited = list(ancestry.values())

            if not inherited and local is None:
                continue

            pure_inheritance = False

            if local and inherited:
                schema = local.acquire_ancestor_inheritance(schema, inherited)
                schema = local.finalize(schema, bases=inherited)
                merged = local

            elif len(inherited) > 1:
                attrs = {'inherited': True}
                schema, merged = type(inherited[0]).derive_from_root(
                    schema,
                    source=self,
                    unqualified_name=classref_key,
                    merge_bases=inherited,
                    attrs=attrs,
                    dctx=dctx,
                )

            elif len(inherited) == 1:
                # Pure inheritance
                item = inherited[0]
                # In some cases pure inheritance is not possible, such
                # as when a pointer has delegated constraints that must
                # be materialized on inheritance.  We delegate the
                # decision to the referenced class here.
                schema, merged = classrefcls.inherit_pure(
                    schema, item, source=self, dctx=dctx)
                pure_inheritance = schema is local_schema

            else:
                # Not inherited
                merged = local

            if (local is not None and inherited and not pure_inheritance and
                    requires_explicit_inherit and
                    not local.get_declared_inherited(local_schema) and
                    dctx is not None and dctx.declarative):
                # locally defined references *must* use
                # the `inherited` keyword if ancestors have
                # a reference under the same name.
                raise errors.SchemaDefinitionError(
                    f'{self.get_shortname(schema)}: '
                    f'{local.get_shortname(local_schema)} must be '
                    f'declared using the `inherited` keyword because '
                    f'it is defined in the following ancestor(s): '
                    f'{", ".join(a.get_shortname(schema) for a in ancestry)}',
                    context=local.get_sourcectx(local_schema)
                )

            if not inherited and local.get_declared_inherited(local_schema):
                raise errors.SchemaDefinitionError(
                    f'{self.get_shortname(schema)}: '
                    f'{local.get_shortname(local_schema)} cannot '
                    f'be declared `inherited` as there are no ancestors '
                    f'defining it.',
                    context=local.get_sourcectx(local_schema)
                )

            if inherited:
                if not pure_inheritance:
                    if dctx is not None:
                        delta = merged.delta(local, merged,
                                             context=None,
                                             old_schema=local_schema,
                                             new_schema=schema)
                        if delta.has_subcommands():
                            dctx.current().op.add(delta)

                    schema, local_classrefs = local_classrefs.update(
                        schema, [merged])

                schema, classrefs = classrefs.update(
                    schema, [merged])

        schema = self.update(schema, {
            attr: classrefs,
            local_attr: local_classrefs
        })

        return schema

    def init_derived(self, schema, source, *qualifiers, as_copy,
                     merge_bases=None, mark_derived=False,
                     replace_original=None, attrs=None, dctx=None,
                     derived_name_base=None,
                     name=None, **kwargs):
        if name is None:
            derived_name = self.get_derived_name(
                schema, source, *qualifiers,
                mark_derived=mark_derived,
                derived_name_base=derived_name_base)
        else:
            derived_name = name

        if as_copy:
            schema, derived = super().init_derived(
                schema, source, *qualifiers, as_copy=True,
                merge_bases=merge_bases, mark_derived=mark_derived,
                attrs=attrs, dctx=dctx, name=name,
                replace_original=replace_original,
                derived_name_base=derived_name_base, **kwargs)

        else:
            if self.get_name(schema) == derived_name:
                raise errors.SchemaError(
                    f'cannot derive {self!r}({derived_name}) from itself')

            derived_attrs = {}

            if attrs is not None:
                derived_attrs.update(attrs)

            if not derived_attrs.get('bases'):
                derived_attrs['bases'] = [self]

            derived_attrs.pop('name', None)

            schema, derived = type(self).create_in_schema(
                schema, name=derived_name, **derived_attrs)

        return schema, derived

    def get_base_names(self, schema):
        return self.get_bases(schema).names(schema)

    def get_topmost_concrete_base(self, schema):
        # Get the topmost non-abstract base.
        for ancestor in reversed(self.compute_mro(schema)):
            if not ancestor.get_is_abstract(schema):
                return ancestor

        raise errors.SchemaError(
            f'{self.get_verbosename(schema)} has no non-abstract ancestors')

    def get_base_for_cast(self, schema):
        if self.is_enum(schema):
            # all enums have to use std::anyenum as base type for casts
            return schema.get('std::anyenum')
        else:
            return self.get_topmost_concrete_base(schema)

    def compute_mro(self, schema):
        return compute_mro(schema, self)

    def _issubclass(self, schema, parent):
        mro = self.compute_mro(schema)
        mro = {o.id for o in mro}

        return parent.id in mro

    def issubclass(self, schema, parent):
        if isinstance(parent, tuple):
            return any(self.issubclass(schema, p) for p in parent)
        else:
            if parent.is_type() and parent.is_any():
                return True
            else:
                return self._issubclass(schema, parent)

    def descendants(self, schema):
        return schema.get_descendants(self)

    def children(self, schema):
        return schema.get_children(self)

    def acquire_ancestor_inheritance(self, schema, bases=None, *, dctx=None):
        if bases is None:
            bases = self.get_bases(schema).objects(schema)

        schema = self.merge(*bases, schema=schema, dctx=dctx)
        return schema

    def update_descendants(self, schema, dctx=None):
        for child in self.children(schema):
            schema = child.acquire_ancestor_inheritance(schema, dctx=dctx)
            schema = child.update_descendants(schema, dctx=dctx)
        return schema

    def finalize(self, schema, bases=None, *, apply_defaults=True, dctx=None):
        schema = super().finalize(
            schema, bases=bases, apply_defaults=apply_defaults,
            dctx=dctx)
        if self.get_ancestors(schema) is None:
            schema = self.set_field_value(
                schema, 'ancestors', compute_mro(schema, self)[1:])
        schema = self.acquire_ancestor_inheritance(schema, dctx=dctx)

        if bases is None:
            bases = self.get_bases(schema).objects(schema)

        for refdict in self.__class__.get_refdicts():
            attr = refdict.attr
            local_attr = refdict.local_attr
            backref_attr = refdict.backref_attr
            ref_cls = refdict.ref_cls
            exp_inh = refdict.requires_explicit_inherit

            schema, ref_keys = self.begin_classref_dict_merge(
                schema, bases=bases, attr=attr)

            schema = self.merge_classref_dict(
                schema, bases=bases, attr=attr,
                local_attr=local_attr,
                backref_attr=backref_attr,
                classrefcls=ref_cls,
                classref_keys=ref_keys,
                requires_explicit_inherit=exp_inh,
                dctx=dctx)

            schema = self.finish_classref_dict_merge(
                schema, bases=bases, attr=attr)

        return schema

    def del_classref(self, schema, collection, key):
        refdict = type(self).get_refdict(collection)
        attr = refdict.attr
        local_attr = refdict.local_attr

        for base in self.get_bases(schema).objects(schema):
            base_coll = base.get_field_value(schema, attr)
            inherited = base_coll.get(schema, key, default=None)
            if inherited is not None:
                break

        if inherited is not None:
            for descendant in self.descendants(schema):
                descendant_local_coll = descendant.get_field_value(
                    schema, local_attr)
                if not descendant_local_coll.has(schema, key):
                    descendant_coll = descendant.get_field_value(schema, attr)
                    schema, descendant_coll = descendant_coll.delete(
                        schema, [key])
                    schema = descendant.set_field_value(
                        schema, attr, descendant_coll)

        schema = super().del_classref(schema, collection, key)
        if inherited is not None:
            all_coll = self.get_field_value(schema, attr)
            schema, all_coll = all_coll.add(schema, inherited)
            schema = self.set_field_value(schema, attr, all_coll)

        return schema

    def get_nearest_non_derived_parent(self, schema):
        obj = self
        while obj.get_derived_from(schema) is not None:
            obj = obj.get_derived_from(schema)
        return obj

    @classmethod
    def get_root_classes(cls):
        return tuple()

    @classmethod
    def get_default_base_name(self):
        return None

    @classmethod
    def create_in_schema_with_inheritance(cls, schema, **kwargs):
        schema, o = cls.create_in_schema(schema, **kwargs)
        schema = o.acquire_ancestor_inheritance(schema)
        return schema, o
