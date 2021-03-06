# -*- coding: utf-8 -*-
##############################################################################
#
#    oerpfs module for OpenERP, Automatic mounts with fuse on the filesystem for simple operations (files access, data import, etc.)
#    Copyright (C) 2014 SYLEAM Info Services (<http://www.Syleam.fr/>)
#              Sylvain Garancher <sylvain.garancher@syleam.fr>
#
#    This file is a part of oerpfs
#
#    oerpfs is free software: you can redistribute it and/or modify
#    it under the terms of the GNU Affero General Public License as published by
#    the Free Software Foundation, either version 3 of the License, or
#    (at your option) any later version.
#
#    oerpfs is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#    GNU Affero General Public License for more details.
#
#    You should have received a copy of the GNU Affero General Public License
#    along with this program.  If not, see <http://www.gnu.org/licenses/>.
#
##############################################################################

import os
import csv
import stat
import fuse
import base64
import multiprocessing
from errno import ENOENT
from StringIO import StringIO
from openerp import pooler
from openerp.osv import orm
from openerp.osv import fields
from openerp.addons.document.document import get_node_context

fuse.fuse_python_api = (0, 2)


class OerpFsDirectory(orm.Model):
    _name = 'oerpfs.directory'
    _description = 'OerpFS Directory'

    _columns = {
        'name': fields.char('Name', size=64, required=True, help='Directory name'),
        'path': fields.char('Path', size=256, required=True, help='Path of this directory'),
        'type': fields.selection([('attachment', 'Attachment'), ('csv_import', 'CSV Import'), ('document', 'Document')], 'Type', required=True, help='Type of mount'),
    }

    _defaults = {
        'path': '/srv/openerp/fs/',
        'type': 'attachment',
    }

    def mount(self, cr, uid, ids, context=None):
        """
        Mount a directory for the choosen user
        """
        def launch(mount_point):
            os.setsid()
            # FIXME : Better manage multi processing
            os.closerange(3, os.sysconf("SC_OPEN_MAX"))
            mount_point.main()

        user = self.pool.get('res.users').browse(cr, uid, uid, context=context)

        for directory in self.browse(cr, uid, ids, context=context):
            fuseClass = None
            if directory.type == 'attachment':
                fuseClass = OerpFSModel
            elif directory.type == 'csv_import':
                fuseClass = OerpFSCsvImport
            elif directory.type == 'document':
                fuseClass = OerpFSDocument

            # Mount options
            mount_options = [
                '-o', 'fsname=oerpfs/' + str(user.login),
                '-o', 'subtype=openerp.' + str(directory.name),
            ]

            # Mount the directory using fuse
            mount_point = fuseClass(uid, cr.dbname)
            mount_point.fuse_args.mountpoint = str(directory.path)
            mount_point.multithreaded = True
            mount_point.parse(mount_options)
            mount_process = multiprocessing.Process(target=launch, args=(mount_point,))
            mount_process.start()

        return True


class OerpFS(fuse.Fuse):
    """
    Base for all OerpFS classes
    """
    def __init__(self, uid, dbname, *args, **kwargs):
        super(OerpFS, self).__init__(*args, **kwargs)

        # Dict used to store files contents
        self.files = {}

        # Initialize OpenERP specific variables
        self.uid = uid
        self.dbname = dbname

    def open(self, path, flags):
        """
        Create a StringIO instance in self.files[path]
        """
        self.files[path] = StringIO()

    def read(self, path, size, offset):
        """
        Return the asked part of the file's contents
        """
        if not path in self.files:
            self.open(path, None)
        self.files[path].seek(offset)
        return self.files[path].read(n=size)

    def create(self, path, mode, fi=None):
        """
        Create an empty StringIO instance in self.files[path]
        Inherited classes may have to create an empty file
        """
        self.files[path] = StringIO()

    def write(self, path, buf, offset):
        """
        Write the contents in the self.files[path] StringIO instance
        """
        if not path in self.files:
            return -ENOENT
        self.files[path].seek(offset)
        self.files[path].write(buf)
        return len(buf)

    def truncate(self, path, length):
        """
        Truncate the file's contents
        """
        self.files[path].truncate(size=length)

    def release(self, path, fh):
        """
        Close the StringIO instance and free memory
        """
        self.files[path].close()
        del self.files[path]


class OerpFSModel(OerpFS):
    """
    Fuse filesystem for simple OpenERP filestore access
    """
    def __init__(self, uid, dbname, *args, **kwargs):
        super(OerpFSModel, self).__init__(uid, dbname, *args, **kwargs)

    def getattr(self, path):
        """
        Return attributes for the specified path :
            - Search for the model as first part
            - Search for an existing record as second part
            - Search for an existing attachment as third part
            - There cannot be more than 3 parts in the path
        """
        db, pool = pooler.get_db_and_pool(self.dbname)
        cr = db.cursor()
        fakeStat = fuse.Stat()
        fakeStat.st_mode = stat.S_IFDIR | 0600
        fakeStat.st_nlink = 0

        if path == '/':
            cr.close()
            return fakeStat

        paths = path.split('/')[1:]
        if len(paths) > 3:
            cr.close()
            return -ENOENT

        # Check for model existence
        model_obj = pool.get('ir.model')
        model_ids = model_obj.search(cr, self.uid, [('model', '=', paths[0])])
        if not model_ids:
            cr.close()
            return -ENOENT
        elif len(paths) == 1:
            cr.close()
            return fakeStat

        # Check for record existence
        element_obj = pool.get(paths[0])
        element_ids = element_obj.search(cr, self.uid, [('id', '=', int(paths[1]))])
        if not element_ids:
            cr.close()
            return -ENOENT
        elif len(paths) == 2:
            cr.close()
            return fakeStat

        # Chech for attachement existence
        attachment_obj = pool.get('ir.attachment')
        attachment_ids = attachment_obj.search(cr, self.uid, [('res_model', '=', paths[0]), ('res_id', '=', int(paths[1])), ('name', '=', paths[2])])
        if not attachment_ids:
            cr.close()
            return -ENOENT

        # Common stats
        fakeStat.st_mode = stat.S_IFREG | 0600
        fakeStat.st_nlink = 2

        # Read the file
        attachment_obj = pool.get('ir.attachment')
        attachment_ids = attachment_obj.search(cr, self.uid, [('res_model', '=', paths[0]), ('res_id', '=', int(paths[1])), ('name', '=', paths[2])])
        attachment_data = attachment_obj.read(cr, self.uid, attachment_ids, ['datas'])
        fakeStat.st_size = attachment_data[0]['datas'] and len(base64.b64decode(attachment_data[0]['datas'])) or 0
        cr.close()
        return fakeStat

    def readdir(self, path, offset):
        """
        Return content of a directory :
            - List models for root path
            - List records for a model
            - List attachments for a record
        We don't have to check for the path, because getattr already returns -ENOENT if the model/record/attachment doesn't exist
        """
        db, pool = pooler.get_db_and_pool(self.dbname)
        cr = db.cursor()
        yield fuse.Direntry('.')
        yield fuse.Direntry('..')

        paths = path.split('/')[1:]
        # List models
        if path == '/':
            model_obj = pool.get('ir.model')
            model_ids = model_obj.search(cr, self.uid, [])
            for model_data in model_obj.read(cr, self.uid, model_ids, ['model']):
                yield fuse.Direntry(str(model_data['model']))
        # List records
        elif len(paths) == 1:
            element_obj = pool.get(paths[0])
            element_ids = element_obj.search(cr, self.uid, [])
            for element_data in element_obj.read(cr, self.uid, element_ids, ['id']):
                yield fuse.Direntry(str(element_data['id']))
        # List attachments
        else:
            attachment_obj = pool.get('ir.attachment')
            attachment_ids = attachment_obj.search(cr, self.uid, [('res_model', '=', paths[0]), ('res_id', '=', int(paths[1]))])
            for attachment_data in attachment_obj.read(cr, self.uid, attachment_ids, ['name']):
                yield fuse.Direntry(str(attachment_data['name']))

        cr.close()

    def rename(self, old_path, new_path):
        """
        Rename a file, eventually moving it from a model to another one, if the parent directories have changed
        """
        db, pool = pooler.get_db_and_pool(self.dbname)
        cr = db.cursor()

        old_paths = old_path.split('/')[1:]
        new_paths = new_path.split('/')[1:]
        attachment_obj = pool.get('ir.attachment')
        attachment_ids = attachment_obj.search(cr, self.uid, [('res_model', '=', old_paths[0]), ('res_id', '=', int(old_paths[1])), ('name', '=', old_paths[2])])
        attachment_obj.write(cr, self.uid, attachment_ids, {'res_model': new_paths[0], 'res_id': new_paths[1], 'name': new_paths[2], 'datas_fname': new_paths[2]})

        cr.commit()
        cr.close()

    def open(self, path, flags):
        """
        Create a StringIO instance in self.files[path], initialized with the file's contents
        """
        super(OerpFSModel, self).open(path, flags)
        db, pool = pooler.get_db_and_pool(self.dbname)
        cr = db.cursor()

        # Read the file's contents
        paths = path.split('/')[1:]
        attachment_obj = pool.get('ir.attachment')
        attachment_ids = attachment_obj.search(cr, self.uid, [('res_model', '=', paths[0]), ('res_id', '=', int(paths[1])), ('name', '=', paths[2])])
        attachment_data = attachment_obj.read(cr, self.uid, attachment_ids, ['datas'])
        if attachment_data[0]['datas']:
            self.files[path] = StringIO(base64.b64decode(attachment_data[0]['datas']))

        cr.close()

    def create(self, path, mode, fi=None):
        """
        Create an empty file
        """
        super(OerpFSModel, self).create(path, mode, fi=fi)

        db, pool = pooler.get_db_and_pool(self.dbname)
        cr = db.cursor()

        # Create an empty ir.attachment file
        paths = path.split('/')[1:]
        attachment_obj = pool.get('ir.attachment')
        attachment_obj.create(cr, self.uid, {'type': 'binary', 'res_model': paths[0], 'res_id': paths[1], 'name': paths[2]})

        cr.commit()
        cr.close()

    def flush(self, path):
        """
        Write the contents into OpenERP
        """
        db, pool = pooler.get_db_and_pool(self.dbname)
        cr = db.cursor()

        # FIXME : Don't know why it doesn't work without rebuilding the StringIO object...
        value = StringIO(self.files[path].getvalue())

        paths = path.split('/')[1:]
        attachment_obj = pool.get('ir.attachment')
        attachment_ids = attachment_obj.search(cr, self.uid, [('res_model', '=', paths[0]), ('res_id', '=', int(paths[1])), ('name', '=', paths[2])])
        attachment_obj.write(cr, self.uid, attachment_ids, {'type': 'binary', 'datas': base64.b64encode(value.getvalue()), 'res_model': paths[0], 'res_id': paths[1], 'name': paths[2], 'datas_fname': paths[2]})

        # Release variables
        value.close()
        del value

        cr.commit()
        cr.close()

    def unlink(self, path):
        """
        Delete a file
        """
        db, pool = pooler.get_db_and_pool(self.dbname)
        cr = db.cursor()

        # Delete the ir.attachment
        paths = path.split('/')[1:]
        attachment_obj = pool.get('ir.attachment')
        attachment_ids = attachment_obj.search(cr, self.uid, [('res_model', '=', paths[0]), ('res_id', '=', int(paths[1])), ('name', '=', paths[2])])
        attachment_obj.unlink(cr, self.uid, attachment_ids)

        cr.commit()
        cr.close()


class OerpFSCsvImport(OerpFS):
    """
    Automatic CSV import to OpenERP on file copy
    """
    def __init__(self, uid, dbname, *args, **kwargs):
        super(OerpFSCsvImport, self).__init__(uid, dbname, *args, **kwargs)

    def getattr(self, path):
        """
        Only the root path exists, where we copy the CSV files to be imported
        """
        fakeStat = fuse.Stat()
        fakeStat.st_mode = stat.S_IFDIR | 0200
        fakeStat.st_nlink = 0

        if path == '/':
            return fakeStat

        if path in self.files:
            fakeStat.st_mode = stat.S_IFREG | 0200
            fakeStat.st_nlink = 1
            return fakeStat

        return -ENOENT

    def readdir(self, path, offset):
        """
        As only the root path exists, we only have to return the default entries
        """
        yield fuse.Direntry('.')
        yield fuse.Direntry('..')

        for path in self.files:
            yield(fuse.Direntry(path))

    def release(self, path, fh):
        """
        Writing of the file is finished, import the contents into OpenERP
        """
        db, pool = pooler.get_db_and_pool(self.dbname)
        cr = db.cursor()
        # FIXME : Don't know why it doesn't work without rebuilding the StringIO object...
        value = StringIO(self.files[path].getvalue())

        # Parse the CSV file contents
        csvFile = csv.reader(value)
        lines = list(csvFile)

        # Import data into OpenERP
        model = path.replace('.csv', '')[1:]
        oerpObject = pool.get(model)
        oerpObject.import_data(cr, self.uid, lines[0], lines[1:], 'init', '', False, {'import': True})

        # Close StringIO and free memory
        self.files[path].close()
        del self.files[path]
        value.close()
        del value

        cr.commit()
        cr.close()

        super(OerpFSCsvImport, self).flush(path)


class OerpFSDocument(OerpFS):
    """
    Fuse filesystem for simple OpenERP documents tree access
    """
    def __init__(self, uid, dbname, *args, **kwargs):
        super(OerpFSDocument, self).__init__(uid, dbname, *args, **kwargs)

    def _get_node(self, cr, path):
        """
        Return a node object corresponding to the supplied path
        """
        if path == '/':
            path = ''

        # Retrieve the node instance
        node = get_node_context(cr, self.uid, {})
        return node.get_uri(cr, path.split('/')[1:])

    def getattr(self, path):
        """
        Return attributes for the specified path
        """
        db, pool = pooler.get_db_and_pool(self.dbname)
        cr = db.cursor()
        node = self._get_node(cr, path)
        if not node:
            cr.close()
            return -ENOENT

        # Default : Declare an unreadable file
        fakeStat = fuse.Stat()
        fakeStat.st_mode = stat.S_IFREG | 0000
        fakeStat.st_nlink = 0

        # Directory
        if node.our_type in ('database', 'collection'):
            fakeStat.st_mode = stat.S_IFDIR | 0600
            cr.close()
            return fakeStat
        # Regular file
        elif node.our_type == 'file':
            fakeStat.st_mode = stat.S_IFREG | 0600
            fakeStat.st_size = node.get_data_len(cr)
            cr.close()
            return fakeStat

        cr.close()
        return fakeStat

    def readdir(self, path, offset):
        """
        Return content of a directory
        We don't have to check for the path, because getattr already returns -ENOENT if the model/record/attachment doesn't exist
        """
        db, pool = pooler.get_db_and_pool(self.dbname)
        cr = db.cursor()
        yield fuse.Direntry('.')
        yield fuse.Direntry('..')

        node = self._get_node(cr, path)
        for child in node.children(cr):
            yield fuse.Direntry(str(child.displayname))

        cr.close()

    def rename(self, old_path, new_path):
        """
        Change the file's path and name
        """
        db, pool = pooler.get_db_and_pool(self.dbname)
        cr = db.cursor()

        # Retrieve the old dir's node instance
        node = self._get_node(cr, old_path)
        # Retrieve the new dir's node instance
        new_dir_node = self._get_node(cr, '/'.join(new_path.split('/')[:-1]))
        # Change the file's name and path
        node.move_to(cr, new_dir_node, new_name=new_path.split('/')[-1])

        cr.commit()
        cr.close()

    def open(self, path, flags):
        """
        Create a StringIO instance in self.files[path], initialized with the file's contents
        """
        super(OerpFSDocument, self).open(path, flags)
        db, pool = pooler.get_db_and_pool(self.dbname)
        cr = db.cursor()

        # Retrieve the file's node instance
        node = self._get_node(cr, path)
        # Read the file's contents
        self.files[path] = StringIO(node.get_data(cr))

        cr.close()

    def create(self, path, mode, fi=None):
        """
        Create an empty file
        """
        super(OerpFSDocument, self).create(path, mode, fi=fi)

        db, pool = pooler.get_db_and_pool(self.dbname)
        cr = db.cursor()

        # Create an empty file
        parent_node = self._get_node(cr, '/'.join(path.split('/')[:-1]))
        parent_node.create_child(cr, path.split('/')[-1])

        cr.commit()
        cr.close()

    def flush(self, path):
        """
        Write the contents into OpenERP
        """
        db, pool = pooler.get_db_and_pool(self.dbname)
        cr = db.cursor()

        # FIXME : Don't know why it doesn't work without rebuilding the StringIO object...
        value = StringIO(self.files[path].getvalue())

        # Write data in the file
        node = self._get_node(cr, path)
        node.set_data(cr, value.getvalue())

        # Release variables
        value.close()
        del value

        cr.commit()
        cr.close()

    def unlink(self, path):
        """
        Delete a file
        """
        db, pool = pooler.get_db_and_pool(self.dbname)
        cr = db.cursor()

        # Delete the file
        node = self._get_node(cr, path)
        node.rm(cr)

        cr.commit()
        cr.close()

# vim:expandtab:smartindent:tabstop=4:softtabstop=4:shiftwidth=4:
