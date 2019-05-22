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


from edb.edgeql import ast as qlast

from edb import errors

from . import delta as sd
from . import inheriting
from . import objects as so
from . import name as sn
from . import utils


class ReferencedObjectCommandMeta(type(sd.ObjectCommand)):
    _transparent_adapter_subclass = True

    def __new__(mcls, name, bases, clsdct, *,
                referrer_context_class=None, **kwargs):
        cls = super().__new__(mcls, name, bases, clsdct, **kwargs)
        if referrer_context_class is not None:
            cls._referrer_context_class = referrer_context_class
        return cls


class ReferencedObjectCommand(sd.ObjectCommand,
                              metaclass=ReferencedObjectCommandMeta):
    _referrer_context_class = None

    @classmethod
    def get_referrer_context_class(cls):
        if cls._referrer_context_class is None:
            raise TypeError(
                f'referrer_context_class is not defined for {cls}')
        return cls._referrer_context_class

    @classmethod
    def get_referrer_context(cls, context):
        return context.get(cls.get_referrer_context_class())

    @classmethod
    def _classname_from_ast(cls, schema, astnode, context):
        name = super()._classname_from_ast(schema, astnode, context)

        parent_ctx = cls.get_referrer_context(context)
        if parent_ctx is not None:
            referrer_name = parent_ctx.op.classname

            try:
                base_ref = utils.ast_to_typeref(
                    qlast.TypeName(maintype=astnode.name),
                    modaliases=context.modaliases, schema=schema)
            except errors.InvalidReferenceError:
                base_name = sn.Name(name)
            else:
                base_name = base_ref.get_name(schema)

            quals = cls._classname_quals_from_ast(
                schema, astnode, base_name, referrer_name, context)
            pnn = sn.get_specialized_name(base_name, referrer_name, *quals)
            name = sn.Name(name=pnn, module=referrer_name.module)

        return name

    @classmethod
    def _classname_quals_from_ast(cls, schema, astnode, base_name,
                                  referrer_name, context):
        return ()

    def _get_ast_node(self, context):
        subject_ctx = self.get_referrer_context(context)
        ref_astnode = getattr(self, 'referenced_astnode', None)
        if subject_ctx is not None and ref_astnode is not None:
            return ref_astnode
        else:
            if isinstance(self.astnode, (list, tuple)):
                return self.astnode[1]
            else:
                return self.astnode

    def _create_innards(self, schema, context):
        schema = super()._create_innards(schema, context)

        referrer_ctx = self.get_referrer_context(context)
        if referrer_ctx is not None:
            referrer = referrer_ctx.scls
            refdict = referrer.__class__.get_refdict_for_class(
                self.scls.__class__)

            if refdict.backref_attr:
                # Set the back-reference on referenced object
                # to the referrer.
                schema = self.scls.set_field_value(
                    schema, refdict.backref_attr, referrer)
                # Add the newly created referenced object to the
                # appropriate refdict in self and all descendants
                # that don't already have an existing reference.
                schema = referrer.add_classref(schema, refdict.attr, self.scls)
                reftype = type(referrer).get_field(refdict.attr).type
                refname = reftype.get_key_for(schema, self.scls)
                for child in referrer.descendants(schema):
                    child_local_coll = child.get_field_value(
                        schema, refdict.local_attr)
                    child_coll = child.get_field_value(schema, refdict.attr)
                    if not child_local_coll.has(schema, refname):
                        schema, child_coll = child_coll.update(
                            schema, [self.scls])
                        schema = child.set_field_value(
                            schema, refdict.attr, child_coll)

        return schema

    def _delete_innards(self, schema, context, scls):
        schema = super()._delete_innards(schema, context, scls)

        referrer_ctx = self.get_referrer_context(context)
        if referrer_ctx is not None:
            referrer = referrer_ctx.scls
            referrer_class = type(referrer)
            refdict = referrer_class.get_refdict_for_class(scls.__class__)
            reftype = referrer_class.get_field(refdict.attr).type
            refname = reftype.get_key_for(schema, self.scls)
            schema = referrer.del_classref(schema, refdict.attr, refname)

            for child in referrer.descendants(schema):
                child_local_coll = child.get_field_value(
                    schema, refdict.local_attr)
                child_coll = child.get_field_value(schema, refdict.attr)
                if not child_local_coll.has(schema, refname):
                    schema, child_coll = child_coll.delete(
                        schema, [refname])
                    schema = child.set_field_value(
                        schema, refdict.attr, child_coll)

        return schema


class ReferencedInheritingObjectCommand(
        ReferencedObjectCommand, inheriting.InheritingObjectCommand):

    def _create_begin(self, schema, context):
        referrer_ctx = self.get_referrer_context(context)
        schema, attrs = self._get_create_fields(schema, context)

        if referrer_ctx is not None and not attrs.get('is_derived'):
            if attrs.get('inherited'):
                self.scls = schema.get(self.classname, None)
            else:
                self.scls = None

            if self.scls is None:
                referrer = referrer_ctx.scls
                bases = self.get_attribute_value('bases')
                if not isinstance(bases, so.ObjectList):
                    bases = so.ObjectList.create(schema, bases)

                bases = list(bases.objects(schema))
                first_base = bases[0]
                merge_bases = bases[1:]

                schema, self.scls = first_base.derive(
                    schema, referrer, attrs=attrs, merge_bases=merge_bases,
                    init_props=False, name=attrs['name'])
            return schema
        else:
            return super()._create_begin(schema, context)


class CreateReferencedInheritingObject(inheriting.CreateInheritingObject):
    @classmethod
    def _cmd_tree_from_ast(cls, schema, astnode, context):
        cmd = super()._cmd_tree_from_ast(schema, astnode, context)

        if isinstance(astnode, cls.referenced_astnode):
            objcls = cls.get_schema_metaclass()

            referrer_ctx = cls.get_referrer_context(context)
            referrer_class = referrer_ctx.op.get_schema_metaclass()
            referrer_name = referrer_ctx.op.classname
            refdict = referrer_class.get_refdict_for_class(objcls)

            cmd.add(
                sd.AlterObjectProperty(
                    property=refdict.backref_attr,
                    new_value=so.ObjectRef(
                        name=referrer_name
                    )
                )
            )

            if getattr(astnode, 'is_abstract', None):
                cmd.add(
                    sd.AlterObjectProperty(
                        property='is_abstract',
                        new_value=True
                    )
                )

        return cmd


class AlterReferencedInheritingObject(
        ReferencedInheritingObjectCommand,
        inheriting.AlterInheritingObject):

    def apply(self, schema, context):

        metaclass = self.get_schema_metaclass()
        obj = schema.get(self.classname, type=metaclass, default=None)
        if obj is None:
            # Many referenced schema items, such as links and properties, are
            # inherited by reference, i.e. no actual item copy is created in a
            # descendant object type. However, when an attempt is made to
            # `ALTER` such property, we must materialize the inherited
            # reference.
            schema, obj = self.materialize_inherited(schema, context)

        return super().apply(schema, context)

    def materialize_inherited(self, schema, context):

        objcls = self.get_schema_metaclass()
        referrer_ctx = self.get_referrer_context(context)
        referrer_class = referrer_ctx.op.get_schema_metaclass()
        referrer = referrer_ctx.scls
        refdict = referrer_class.get_refdict_for_class(objcls)
        refs = referrer.get_field_value(schema, refdict.attr)
        key = refs.get_key_for_name(schema, self.classname)
        inherited_obj = refs.get(schema, key)
        old_schema = schema
        schema, obj = inherited_obj.derive_copy(
            schema,
            referrer,
            dctx=context,
            attrs=dict(
                inherited=True,
            )
        )

        create_delta = objcls.delta(
            None, obj, old_schema=old_schema, new_schema=schema)

        referrer_ctx.op.prepend(create_delta)

        return schema, obj
