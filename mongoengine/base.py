from queryset import QuerySet, QuerySetManager

import pymongo


class ValidationError(Exception):
    pass


class BaseField(object):
    """A base class for fields in a MongoDB document. Instances of this class
    may be added to subclasses of `Document` to define a document's schema.
    """
    
    def __init__(self, name=None, required=False, default=None, unique=False,
                 unique_with=None, primary_key=False):
        self.name = name if not primary_key else '_id'
        self.required = required or primary_key
        self.default = default
        self.unique = bool(unique or unique_with)
        self.unique_with = unique_with
        self.primary_key = primary_key

    def __get__(self, instance, owner):
        """Descriptor for retrieving a value from a field in a document. Do 
        any necessary conversion between Python and MongoDB types.
        """
        if instance is None:
            # Document class being used rather than a document object
            return self

        # Get value from document instance if available, if not use default
        value = instance._data.get(self.name)
        if value is None:
            value = self.default
            # Allow callable default values
            if callable(value):
                value = value()
        return value

    def __set__(self, instance, value):
        """Descriptor for assigning a value to a field in a document.
        """
        instance._data[self.name] = value

    def to_python(self, value):
        """Convert a MongoDB-compatible type to a Python type.
        """
        return value

    def to_mongo(self, value):
        """Convert a Python type to a MongoDB-compatible type.
        """
        return self.to_python(value)

    def prepare_query_value(self, value):
        """Prepare a value that is being used in a query for PyMongo.
        """
        return value

    def validate(self, value):
        """Perform validation on a value.
        """
        pass


class ObjectIdField(BaseField):
    """An field wrapper around MongoDB's ObjectIds.
    """
    
    def to_python(self, value):
        return unicode(value)

    def to_mongo(self, value):
        if not isinstance(value, pymongo.objectid.ObjectId):
            return pymongo.objectid.ObjectId(str(value))
        return value

    def prepare_query_value(self, value):
        return self.to_mongo(value)

    def validate(self, value):
        try:
            pymongo.objectid.ObjectId(str(value))
        except:
            raise ValidationError('Invalid Object ID')


class DocumentMetaclass(type):
    """Metaclass for all documents.
    """

    def __new__(cls, name, bases, attrs):
        metaclass = attrs.get('__metaclass__')
        super_new = super(DocumentMetaclass, cls).__new__
        if metaclass and issubclass(metaclass, DocumentMetaclass):
            return super_new(cls, name, bases, attrs)

        doc_fields = {}
        class_name = [name]
        superclasses = {}
        for base in bases:
            # Include all fields present in superclasses
            if hasattr(base, '_fields'):
                doc_fields.update(base._fields)
                class_name.append(base._class_name)
                # Get superclasses from superclass
                superclasses[base._class_name] = base
                superclasses.update(base._superclasses)
        attrs['_class_name'] = '.'.join(reversed(class_name))
        attrs['_superclasses'] = superclasses

        # Add the document's fields to the _fields attribute
        for attr_name, attr_value in attrs.items():
            if hasattr(attr_value, "__class__") and \
                issubclass(attr_value.__class__, BaseField):
                if not attr_value.name:
                    attr_value.name = attr_name
                doc_fields[attr_name] = attr_value
        attrs['_fields'] = doc_fields

        return super_new(cls, name, bases, attrs)


class TopLevelDocumentMetaclass(DocumentMetaclass):
    """Metaclass for top-level documents (i.e. documents that have their own
    collection in the database.
    """

    def __new__(cls, name, bases, attrs):
        super_new = super(TopLevelDocumentMetaclass, cls).__new__
        # Classes defined in this package are abstract and should not have 
        # their own metadata with DB collection, etc.
        # __metaclass__ is only set on the class with the __metaclass__ 
        # attribute (i.e. it is not set on subclasses). This differentiates
        # 'real' documents from the 'Document' class
        if attrs.get('__metaclass__') == TopLevelDocumentMetaclass:
            return super_new(cls, name, bases, attrs)

        collection = name.lower()
        
        simple_class = True
        id_field = None
        base_indexes = []

        # Subclassed documents inherit collection from superclass
        for base in bases:
            if hasattr(base, '_meta') and 'collection' in base._meta:
                # Ensure that the Document class may be subclassed - 
                # inheritance may be disabled to remove dependency on 
                # additional fields _cls and _types
                if base._meta.get('allow_inheritance', True) == False:
                    raise ValueError('Document %s may not be subclassed' %
                                     base.__name__)
                else:
                    simple_class = False
                collection = base._meta['collection']

                id_field = id_field or base._meta.get('id_field')
                base_indexes += base._meta.get('indexes', [])

        meta = {
            'collection': collection,
            'allow_inheritance': True,
            'max_documents': None,
            'max_size': None,
            'ordering': [], # default ordering applied at runtime
            'indexes': [], # indexes to be ensured at runtime
            'id_field': id_field,
        }

        # Apply document-defined meta options
        meta.update(attrs.get('meta', {}))

        meta['indexes'] += base_indexes
        
        # Only simple classes - direct subclasses of Document - may set
        # allow_inheritance to False
        if not simple_class and not meta['allow_inheritance']:
            raise ValueError('Only direct subclasses of Document may set '
                             '"allow_inheritance" to False')
        attrs['_meta'] = meta

        # Set up collection manager, needs the class to have fields so use
        # DocumentMetaclass before instantiating CollectionManager object
        new_class = super_new(cls, name, bases, attrs)
        new_class.objects = QuerySetManager()

        unique_indexes = []
        for field_name, field in new_class._fields.items():
            # Generate a list of indexes needed by uniqueness constraints
            if field.unique:
                field.required = True
                unique_fields = [field_name]

                # Add any unique_with fields to the back of the index spec
                if field.unique_with:
                    if isinstance(field.unique_with, basestring):
                        field.unique_with = [field.unique_with]

                    # Convert unique_with field names to real field names
                    unique_with = []
                    for other_name in field.unique_with:
                        parts = other_name.split('.')
                        # Lookup real name
                        parts = QuerySet._lookup_field(new_class, parts)
                        name_parts = [part.name for part in parts]
                        unique_with.append('.'.join(name_parts))
                        # Unique field should be required
                        parts[-1].required = True
                    unique_fields += unique_with

                # Add the new index to the list
                index = [(f, pymongo.ASCENDING) for f in unique_fields]
                unique_indexes.append(index)

            # Check for custom primary key
            if field.primary_key:
                if not new_class._meta['id_field']:
                    new_class._meta['id_field'] = field_name
                    # Make 'Document.id' an alias to the real primary key field
                    new_class.id = field
                    #new_class._fields['id'] = field
                else:
                    raise ValueError('Cannot override primary key field')

        new_class._meta['unique_indexes'] = unique_indexes

        if not new_class._meta['id_field']:
            new_class._meta['id_field'] = 'id'
            new_class.id = new_class._fields['id'] = ObjectIdField(name='_id')

        return new_class


class BaseDocument(object):

    def __init__(self, **values):
        self._data = {}
        # Assign initial values to instance
        for attr_name, attr_value in self._fields.items():
            if attr_name in values:
                setattr(self, attr_name, values.pop(attr_name))
            else:
                # Use default value if present
                value = getattr(self, attr_name, None)
                setattr(self, attr_name, value)

    @classmethod
    def _get_subclasses(cls):
        """Return a dictionary of all subclasses (found recursively).
        """
        try:
            subclasses = cls.__subclasses__()
        except:
            subclasses = cls.__subclasses__(cls)

        all_subclasses = {}
        for subclass in subclasses:
            all_subclasses[subclass._class_name] = subclass
            all_subclasses.update(subclass._get_subclasses())
        return all_subclasses

    def __iter__(self):
        return iter(self._fields)

    def __getitem__(self, name):
        """Dictionary-style field access, return a field's value if present.
        """
        try:
            if name in self._fields:
                return getattr(self, name)
        except AttributeError:
            pass
        raise KeyError(name)

    def __setitem__(self, name, value):
        """Dictionary-style field access, set a field's value.
        """
        # Ensure that the field exists before settings its value
        if name not in self._fields:
            raise KeyError(name)
        return setattr(self, name, value)

    def __contains__(self, name):
        try:
            val = getattr(self, name)
            return val is not None
        except AttributeError:
            return False

    def __len__(self):
        return len(self._data)

    def __repr__(self):
        try:
            u = unicode(self)
        except (UnicodeEncodeError, UnicodeDecodeError):
            u = '[Bad Unicode data]'
        return u'<%s: %s>' % (self.__class__.__name__, u)

    def __str__(self):
        if hasattr(self, '__unicode__'):
            return unicode(self).encode('utf-8')
        return '%s object' % self.__class__.__name__

    def to_mongo(self):
        """Return data dictionary ready for use with MongoDB.
        """
        data = {}
        for field_name, field in self._fields.items():
            value = getattr(self, field_name, None)
            if value is not None:
                data[field.name] = field.to_mongo(value)
        # Only add _cls and _types if allow_inheritance is not False
        if not (hasattr(self, '_meta') and
                self._meta.get('allow_inheritance', True) == False):
            data['_cls'] = self._class_name
            data['_types'] = self._superclasses.keys() + [self._class_name]
        return data
    
    @classmethod
    def _from_son(cls, son):
        """Create an instance of a Document (subclass) from a PyMongo SOM.
        """
        # get the class name from the document, falling back to the given
        # class if unavailable
        class_name = son.get(u'_cls', cls._class_name)

        data = dict((str(key), value) for key, value in son.items())

        if '_types' in data:
            del data['_types']

        if '_cls' in data:
            del data['_cls']

        # Return correct subclass for document type
        if class_name != cls._class_name:
            subclasses = cls._get_subclasses()
            if class_name not in subclasses:
                # Type of document is probably more generic than the class
                # that has been queried to return this SON
                return None
            cls = subclasses[class_name]

        for field_name, field in cls._fields.items():
            if field.name in data:
                data[field_name] = field.to_python(data[field.name])

        return cls(**data)